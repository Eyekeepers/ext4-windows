"""Write the way an agent's drive is written, until the disk is pulled.

    python -m yank.workload --target <dir on the disk> --acklog <file elsewhere>
                            [--pg-bin <dir> --pg-data <dir> --pg-socket <dir> --pg-port N]
                            [--seed N] [--duration SECONDS]

Each operation is one of the patterns real software on the drive uses, with
exactly the durability that software asks for -- no more, because the harness
must measure what applications actually get, and no less, because a claim the
software never made is not one the drive owes it:

  credential        written once, fsync'd, with its folder: must always be intact
  replace/durable   temp file, fsync, rename, fsync the folder: acknowledged
                    versions must survive
  replace/app       temp file, fsync, rename (how careful apps save):
                    the file must be a whole version, never torn
  replace/none      temp file, rename, no fsync at all (seed.py's _write, and
                    most Node code): the file must still never be empty or torn
  append            one line, fsync'd: acknowledged lines must all be there
  sqlite/rollback   journal_mode=DELETE, synchronous=EXTRA: acknowledged rows survive.
                    Not FULL: in DELETE mode the commit is the journal's
                    deletion, and only EXTRA syncs the folder after it, so FULL
                    may roll back the last commit on a power cut. SQLite
                    documents this, and the first Linux reference run lost
                    exactly that one row (2026-10-02)
  sqlite/wal        journal_mode=WAL, synchronous=FULL: acknowledged rows survive
  copy              a large file copied in and fsync'd: acknowledged copies match
  pg                PostgreSQL commits: acknowledged rows survive

The process is killed, not stopped, when the disk goes: nothing here cleans up.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .acklog import AckLog
from .pg import TABLE, Postgres, ensure_schema

REPLACE_FILES = {"durable": "config/durable.json", "app": "config/app.json", "none": "config/none.json"}
APPEND_FILE = "memory/MEMORY.md"
CREDENTIAL = "secrets/credentials.json"
SQLITE = {"rollback": ("db/rollback.sqlite", "DELETE", "EXTRA"), "wal": ("db/wal.sqlite", "WAL", "FULL")}
COPY_LIMIT = 6
COPY_SIZE = 8 * 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_dir(path: Path) -> None:
    # A folder can be fsync'd on POSIX and not on Windows, where Python cannot
    # open one. On Windows the rename's durability is the filesystem's to give,
    # which is what an application there gets, so that is what is measured.
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes, fsync: bool) -> None:
    with open(path, "wb") as stream:
        stream.write(data)
        if fsync:
            stream.flush()
            os.fsync(stream.fileno())


class Workload:
    def __init__(self, target: Path, log: AckLog, rng: random.Random, pg: Postgres | None, pg_id_base: int):
        self.target = target
        self.log = log
        self.rng = rng
        self.pg = pg
        self.pg_session = None
        self.pg_next = pg_id_base
        self.versions = {name: 0 for name in REPLACE_FILES}
        self.append_seq = 0
        self.sqlite_next = {name: 0 for name in SQLITE}
        self.connections: dict[str, sqlite3.Connection] = {}
        self.copies = 0
        self.copy_source: Path | None = None

    # -- setup: everything here is acknowledged before the first pull can come

    def setup(self) -> None:
        for folder in ("config", "memory", "secrets", "db", "files"):
            (self.target / folder).mkdir(parents=True, exist_ok=True)
        fsync_dir(self.target)

        credential = json.dumps({"token": self.rng.randbytes(24).hex()}, indent=2).encode()
        path = self.target / CREDENTIAL
        write_file(path, credential, fsync=True)
        os.chmod(path, 0o600)
        fsync_dir(path.parent)
        self.log.ack("credential", path=CREDENTIAL, sha=sha(credential))

        for durability in REPLACE_FILES:
            self.replace(durability, initial=True)

        (self.target / APPEND_FILE).touch()
        fsync_dir(self.target / "memory")

        for name, (rel, mode, synchronous) in SQLITE.items():
            connection = sqlite3.connect(self.target / rel, isolation_level=None)
            connection.execute(f"PRAGMA journal_mode={mode}")
            # Say so if the mode did not take. SQLite answers a journal_mode it
            # refused with the mode it kept instead rather than failing, so a run
            # can believe it is testing WAL while testing DELETE -- which is the
            # mode programs fall back to when WAL fails, and the dangerous one on Windows.
            got = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if got.lower() != mode.lower():
                raise RuntimeError(f"{name}: asked for journal_mode={mode}, this filesystem gave {got}")
            connection.execute(f"PRAGMA synchronous={synchronous}")
            connection.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, sha TEXT NOT NULL, payload TEXT NOT NULL)")
            self.connections[name] = connection
        fsync_dir(self.target / "db")

        if self.pg is not None:
            # The server log goes to the host: it must survive the pull to explain it.
            self.pg.start(Path(tempfile.gettempdir()) / f"yank-pg-{os.getpid()}.log")
            ensure_schema(self.pg)
            self.pg_session = self.pg.session()

        source = Path(tempfile.gettempdir()) / f"yank-copy-source-{os.getpid()}.bin"
        source.write_bytes(self.rng.randbytes(COPY_SIZE))
        self.copy_source = source
        self.log.note("ready")

    # -- operations

    def replace(self, durability: str, initial: bool = False) -> None:
        rel = REPLACE_FILES[durability]
        version = self.versions[durability] + (0 if initial else 1)
        body = json.dumps({"version": version, "durability": durability,
                           "padding": self.rng.randbytes(self.rng.randint(64, 4096)).hex()}).encode()
        self.log.intent("replace", path=rel, durability=durability, version=version, sha=sha(body))
        path = self.target / rel
        temporary = path.with_name(path.name + ".tmp")
        write_file(temporary, body, fsync=durability in ("durable", "app") or initial)
        os.replace(temporary, path)
        if durability == "durable" or initial:
            fsync_dir(path.parent)
            self.log.ack("replace", path=rel, durability=durability, version=version)
        self.versions[durability] = version

    def append(self) -> None:
        line = f"- note {self.append_seq}: {self.rng.randbytes(self.rng.randint(8, 200)).hex()}\n"
        self.log.intent("append", path=APPEND_FILE, seq=self.append_seq, line=line)
        with open(self.target / APPEND_FILE, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self.log.ack("append", path=APPEND_FILE, seq=self.append_seq)
        self.append_seq += 1

    def sqlite(self, name: str) -> None:
        row_id = self.sqlite_next[name]
        payload = self.rng.randbytes(self.rng.randint(16, 2048)).hex()
        self.connections[name].execute("INSERT INTO rows (id, sha, payload) VALUES (?, ?, ?)",
                                       (row_id, sha(payload.encode()), payload))
        self.log.ack("sqlite", db=name, id=row_id, sha=sha(payload.encode()))
        self.sqlite_next[name] = row_id + 1

    def copy(self) -> None:
        if self.copies >= COPY_LIMIT or self.copy_source is None:
            return
        rel = f"files/copy-{self.copies}.bin"
        data = self.copy_source.read_bytes()
        self.log.intent("copy", path=rel, sha=sha(data))
        destination = self.target / rel
        shutil.copyfile(self.copy_source, destination)
        with open(destination, "rb+") as stream:
            os.fsync(stream.fileno())
        fsync_dir(destination.parent)
        self.log.ack("copy", path=rel)
        self.copies += 1

    def postgres(self) -> None:
        if self.pg_session is None:
            return
        row_id = self.pg_next
        payload = self.rng.randbytes(self.rng.randint(16, 2048)).hex()
        session = self.pg_session
        session.stdin.write(f"INSERT INTO {TABLE} (id, sha, payload) VALUES ({row_id}, '{sha(payload.encode())}', '{payload}');\n"
                            f"\\echo ack {row_id}\n")
        session.stdin.flush()
        while True:
            line = session.stdout.readline()
            if not line:
                raise RuntimeError("psql ended")
            if line.strip() == f"ack {row_id}":
                break
            if "ERROR" in line or "FATAL" in line or "PANIC" in line:
                raise RuntimeError(line.strip())
        self.log.ack("pg", id=row_id, sha=sha(payload.encode()))
        self.pg_next = row_id + 1

    def step(self) -> None:
        choice = self.rng.choices(
            ["durable", "app", "none", "append", "rollback", "wal", "copy", "pg"],
            weights=[10, 10, 10, 15, 15, 15, 2, 15 if self.pg_session else 0])[0]
        if choice in REPLACE_FILES:
            self.replace(choice)
        elif choice == "append":
            self.append()
        elif choice in SQLITE:
            self.sqlite(choice)
        elif choice == "copy":
            self.copy()
        else:
            self.postgres()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--acklog", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--duration", type=float, default=0, help="stop by itself after this many seconds (0: run until killed)")
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--pg-data", type=Path)
    parser.add_argument("--pg-socket", type=Path)
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-id-base", type=int, default=0)
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    log = AckLog(args.acklog)
    log.note("start", seed=seed, target=str(args.target), pid=os.getpid())
    pg = Postgres(args.pg_bin, args.pg_data, args.pg_socket, args.pg_port) if args.pg_bin else None
    workload = Workload(args.target, log, random.Random(seed), pg, args.pg_id_base)
    workload.setup()
    deadline = time.monotonic() + args.duration if args.duration else None
    while deadline is None or time.monotonic() < deadline:
        workload.step()
    log.note("finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
