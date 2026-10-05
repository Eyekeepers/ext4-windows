"""Check a recovered drive against the record of what was saved on it.

    python -m yank.verify --run <target> <acklog> [--run ...]
                          [--pg-bin <dir> --pg-data <dir> --pg-socket <dir> --pg-port N]

Run after the drive has been mounted again, so the filesystem has replayed its
journal and the databases can run their own crash recovery. Prints one JSON
object and exits 0 when every claim in every ack log holds, 1 otherwise.

What is a failure is exactly what the workload's docstring promises, nothing
stricter: a write that was never acknowledged may be missing, but nothing
acknowledged may be, and no atomically replaced file may ever be torn.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from . import acklog
from .pg import TABLE, Postgres
from .workload import SQLITE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def as_dict(self) -> dict:
        return {"ok": not self.failures, "failures": self.failures,
                "checked": dict(sorted(self.counts.items())), "notes": self.notes}


def verify_run(target: Path, records: list[dict], report: Report) -> None:
    intents = [r for r in records if r.get("op") == "intent"]
    acks = [r for r in records if r.get("op") == "ack"]
    label = target.name

    for ack in (a for a in acks if a["kind"] == "credential"):
        path = target / ack["path"]
        if not path.is_file() or sha(path.read_bytes()) != ack["sha"]:
            report.fail(f"{label}: credential {ack['path']} is missing or changed")
        report.counts["credential"] += 1

    # Replaced files: always a whole version that was written, and for the
    # durable pattern never older than the newest acknowledged one.
    written: dict[str, dict[str, int]] = defaultdict(dict)
    for intent in (i for i in intents if i["kind"] == "replace"):
        written[intent["path"]][intent["sha"]] = intent["version"]
    newest_ack: dict[str, int] = {}
    for ack in (a for a in acks if a["kind"] == "replace"):
        newest_ack[ack["path"]] = max(newest_ack.get(ack["path"], -1), ack["version"])
    for rel, versions in written.items():
        path = target / rel
        durability = next(i["durability"] for i in intents if i["kind"] == "replace" and i["path"] == rel)
        if not path.is_file():
            if rel in newest_ack:
                report.fail(f"{label}: {rel} ({durability}) is gone; version {newest_ack[rel]} was acknowledged")
            continue
        data = path.read_bytes()
        found = versions.get(sha(data))
        if found is None:
            shape = "empty" if not data else "torn or unknown"
            report.fail(f"{label}: {rel} ({durability}) is {shape} — {len(data)} bytes matching no version written")
            continue
        acked = newest_ack.get(rel, -1)
        if durability == "durable" and found < acked:
            report.fail(f"{label}: {rel} went back to version {found}; version {acked} was acknowledged")
        elif found < acked:
            report.notes.append(f"{label}: {rel} ({durability}) holds version {found}, older than {acked} — allowed for this pattern")
        report.counts[f"replace/{durability}"] += 1

    # Appends: every acknowledged line, in order, then at most part of the next.
    lines: dict[str, dict[int, str]] = defaultdict(dict)
    for intent in (i for i in intents if i["kind"] == "append"):
        lines[intent["path"]][intent["seq"]] = intent["line"]
    acked_seq: dict[str, int] = {}
    for ack in (a for a in acks if a["kind"] == "append"):
        acked_seq[ack["path"]] = max(acked_seq.get(ack["path"], -1), ack["seq"])
    for rel, by_seq in lines.items():
        path = target / rel
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if text is None:
            if rel in acked_seq:
                report.fail(f"{label}: {rel} is gone; {acked_seq[rel] + 1} lines were acknowledged")
            continue
        position, seq = 0, 0
        while seq in by_seq and text.startswith(by_seq[seq], position):
            position += len(by_seq[seq])
            seq += 1
        rest = text[position:]
        if seq <= acked_seq.get(rel, -1):
            report.fail(f"{label}: {rel} lost acknowledged line {seq}; {acked_seq[rel] + 1} were acknowledged")
        elif rest and not (seq in by_seq and by_seq[seq].startswith(rest)):
            report.fail(f"{label}: {rel} has {len(rest)} unexpected bytes after line {seq - 1}")
        report.counts["append-lines"] += seq

    for name, (rel, _mode, _synchronous) in SQLITE.items():
        rows = {a["id"]: a["sha"] for a in acks if a["kind"] == "sqlite" and a["db"] == name}
        path = target / rel
        if not path.is_file():
            if rows:
                report.fail(f"{label}: sqlite/{name} is gone with {len(rows)} acknowledged rows")
            continue
        try:
            connection = sqlite3.connect(path)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            present = dict(connection.execute("SELECT id, sha FROM rows").fetchall())
            connection.close()
        except sqlite3.Error as error:
            report.fail(f"{label}: sqlite/{name} cannot be read: {error}")
            continue
        if integrity != "ok":
            report.fail(f"{label}: sqlite/{name} integrity_check: {integrity}")
        missing = [i for i, digest in rows.items() if present.get(i) != digest]
        if missing:
            report.fail(f"{label}: sqlite/{name} lost {len(missing)} acknowledged rows, first id {min(missing)}")
        report.counts[f"sqlite/{name}"] += len(rows)

    expected = {i["path"]: i["sha"] for i in intents if i["kind"] == "copy"}
    for ack in (a for a in acks if a["kind"] == "copy"):
        path = target / ack["path"]
        if not path.is_file() or sha(path.read_bytes()) != expected[ack["path"]]:
            report.fail(f"{label}: acknowledged copy {ack['path']} is missing or differs")
        report.counts["copy"] += 1


def verify_postgres(pg: Postgres, rows: dict[int, str], report: Report, log: Path) -> None:
    try:
        pg.start(log)  # crash recovery happens here
    except RuntimeError as error:
        report.fail(f"postgres does not start after the pull: {error}")
        return
    try:
        present = {int(i): digest for i, digest in pg.query(f"SELECT id, sha FROM {TABLE}")}
        # The same rows through the primary-key index, so a heap and an index
        # that disagree after recovery are caught.
        indexed = pg.query(f"SET enable_seqscan = off; SET enable_bitmapscan = off; "
                           f"SELECT count(*) FROM {TABLE} WHERE id >= 0")
        if int(indexed[-1][0]) != len(present):
            report.fail(f"postgres index and table disagree: {indexed[-1][0]} vs {len(present)} rows")
    finally:
        pg.stop()
    missing = [i for i, digest in rows.items() if present.get(i) != digest]
    if missing:
        report.fail(f"postgres lost {len(missing)} acknowledged commits, first id {min(missing)}")
    ok, detail = pg.checksums()
    if not ok:
        report.fail(f"postgres page checksums: {detail.splitlines()[-1] if detail else 'failed'}")
    report.counts["pg"] += len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run", nargs=2, action="append", metavar=("TARGET", "ACKLOG"), required=True)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--pg-data", type=Path)
    parser.add_argument("--pg-socket", type=Path)
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-log", type=Path, default=Path("/tmp/yank-verify-pg.log"))
    args = parser.parse_args()

    report = Report()
    pg_rows: dict[int, str] = {}
    for target, log in args.run:
        records = acklog.read(Path(log))
        verify_run(Path(target), records, report)
        pg_rows.update({r["id"]: r["sha"] for r in records if r.get("op") == "ack" and r["kind"] == "pg"})
    if args.pg_bin:
        verify_postgres(Postgres(args.pg_bin, args.pg_data, args.pg_socket, args.pg_port), pg_rows, report, args.pg_log)
    print(json.dumps(report.as_dict(), indent=2))
    return 0 if not report.failures else 1


if __name__ == "__main__":
    sys.exit(main())
