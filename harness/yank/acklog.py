"""The record of what was written and what was acknowledged as saved.

It lives on a different disk from the one being pulled -- that is the point of
it -- and every line is flushed to that disk before the next write begins, so a
line in this log is a claim the drive has to honour.

Two kinds of line:

  intent  written *before* an operation, naming what is about to be written.
          The verifier needs it to recognise a version or an append that
          reached the drive but was never acknowledged.
  ack     written *after* the operation returned and its durability call
          (fsync, commit) succeeded. Anything acknowledged must survive.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class AckLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")

    def _write(self, record: dict) -> None:
        record["t"] = round(time.time(), 6)
        self._file.write(json.dumps(record, sort_keys=True) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def intent(self, kind: str, **fields) -> None:
        self._write({"op": "intent", "kind": kind, **fields})

    def ack(self, kind: str, **fields) -> None:
        self._write({"op": "ack", "kind": kind, **fields})

    def note(self, kind: str, **fields) -> None:
        self._write({"op": "note", "kind": kind, **fields})

    def close(self) -> None:
        self._file.close()


def read(path: Path) -> list[dict]:
    """Every complete line. The harness can be killed mid-line, and a torn last
    line is the one record that never made a claim, so it is skipped."""
    records = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    for line in text.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
