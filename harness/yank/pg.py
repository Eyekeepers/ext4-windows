"""A PostgreSQL cluster on the disk under test, as the product's memory runs one.

The binaries are the product's own (the payload's PostgreSQL 16), never the
computer's. The cluster is created with page checksums so that after a pull
`pg_checksums --check` reads every page and names any that came back torn --
a stronger check than the queries alone, and the payload carries no amcheck.

The socket lives on the host, not on the disk under test: a pulled disk must
not take the harness's own way of talking to the database with it.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

USER = "harness"
TABLE = "acks"


class Postgres:
    def __init__(self, bin_dir: Path, data: Path, socket_dir: Path, port: int) -> None:
        self.bin = bin_dir
        self.data = data
        self.socket_dir = socket_dir
        self.port = port
        self.server: subprocess.Popen | None = None

    def _cmd(self, name: str) -> str:
        return str(self.bin / name)

    def client_args(self) -> list[str]:
        return ["-h", str(self.socket_dir), "-p", str(self.port), "-U", USER]

    def initdb(self) -> None:
        subprocess.run([self._cmd("initdb"), "-D", str(self.data), "-U", USER, "-A", "trust",
                        "-E", "UTF8", "--no-locale", "--data-checksums"],
                       check=True, stdout=subprocess.DEVNULL)

    def start(self, log: Path) -> None:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log, "ab")
        # A direct child, not pg_ctl's detached daemon: the driver kills the
        # whole process group at the moment of the pull, and the server has
        # to be in it.
        self.server = subprocess.Popen(
            [self._cmd("postgres"), "-D", str(self.data), "-p", str(self.port), "-k", str(self.socket_dir),
             "-c", "listen_addresses=", "-c", "fsync=on", "-c", "synchronous_commit=on",
             "-c", "full_page_writes=on"],
            stdout=stream, stderr=stream, stdin=subprocess.DEVNULL)
        stream.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError(f"postgres exited during startup; see {log}")
            ready = subprocess.run([self._cmd("pg_isready"), *self.client_args(), "-d", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError("postgres did not become ready")

    def query(self, sql: str) -> list[list[str]]:
        result = subprocess.run([self._cmd("psql"), "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                                 *self.client_args(), "-d", "postgres", "-c", sql],
                                check=True, capture_output=True, text=True)
        return [line.split("\t") for line in result.stdout.splitlines() if line]

    def session(self) -> subprocess.Popen:
        """One long psql, fed statements on stdin: a commit is acknowledged when
        the `\\echo` after it comes back, which psql runs only once the
        statement has returned."""
        return subprocess.Popen([self._cmd("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", *self.client_args(),
                                 "-d", "postgres"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)

    def stop(self) -> None:
        subprocess.run([self._cmd("pg_ctl"), "stop", "-D", str(self.data), "-m", "fast", "-w", "-t", "60"],
                       check=True, stdout=subprocess.DEVNULL)
        if self.server is not None:
            self.server.wait(timeout=30)

    def checksums(self) -> tuple[bool, str]:
        result = subprocess.run([self._cmd("pg_checksums"), "--check", "-D", str(self.data)],
                                capture_output=True, text=True)
        return result.returncode == 0, (result.stdout + result.stderr).strip()


def ensure_schema(pg: Postgres) -> None:
    pg.query(f"CREATE TABLE IF NOT EXISTS {TABLE} (id bigint PRIMARY KEY, sha text NOT NULL, payload text NOT NULL)")
