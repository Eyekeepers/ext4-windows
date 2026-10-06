"""Does SQLite's WAL mode actually engage on an ext4win mount, and do its locks work?

WAL needs shared memory (the -shm file, memory-mapped) and byte-range locks.
Programs such as Hermes Agent drop their databases to journal_mode=DELETE when
those fail on a FUSE-like filesystem, and DELETE is the mode that loses an acknowledged
commit on a pull unless the filesystem syncs the folder after the journal is
removed. So whether WAL works here decides which risk Windows carries.
"""
import sqlite3, subprocess, sys, time, os
from pathlib import Path

root = Path(sys.argv[1])
db = root / "wal-probe.sqlite"
out = {}

conn = sqlite3.connect(db, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
conn.execute("PRAGMA synchronous=FULL")
conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
conn.execute("INSERT INTO t (v) VALUES ('one')")
out["sidecars_after_write"] = sorted(p.name for p in root.glob("wal-probe.sqlite-*"))

# Shared memory: a second connection in this process must see the first's
# uncheckpointed commit, which it can only do through the -shm index.
second = sqlite3.connect(db, isolation_level=None)
out["second_connection_sees_it"] = second.execute("SELECT v FROM t").fetchone()[0] == "one"

# Byte-range locks: a writer holding a transaction must make another writer wait.
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO t (v) VALUES ('two')")
third = sqlite3.connect(db, timeout=0.2, isolation_level=None)
try:
    third.execute("BEGIN IMMEDIATE")
    out["write_lock_excludes_second_writer"] = False
except sqlite3.OperationalError as exc:
    out["write_lock_excludes_second_writer"] = "locked" in str(exc) or "busy" in str(exc)
# A reader must still see the old value while the writer is mid-transaction.
out["reader_not_blocked_by_writer"] = second.execute("SELECT count(*) FROM t").fetchone()[0] == 1
conn.execute("COMMIT")
out["after_commit_rows"] = second.execute("SELECT count(*) FROM t").fetchone()[0]

# Checkpoint, the operation that rewrites the main file from the -wal.
out["checkpoint"] = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
conn.execute("PRAGMA integrity_check").fetchone()
out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]

# And the mode programs fall back to, so we know it is usable at all.
conn2 = sqlite3.connect(root / "delete-probe.sqlite", isolation_level=None)
conn2.execute("PRAGMA journal_mode=DELETE")
out["delete_mode"] = conn2.execute("PRAGMA journal_mode").fetchone()[0]
conn2.execute("PRAGMA synchronous=EXTRA")
conn2.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
conn2.execute("INSERT INTO t (id) VALUES (1)")
out["delete_mode_works"] = conn2.execute("SELECT count(*) FROM t").fetchone()[0] == 1

for key, value in out.items():
    print(f"{key}: {value}")
