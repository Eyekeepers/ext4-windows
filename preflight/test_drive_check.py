#!/usr/bin/env python3
"""Throwaway disk images, each with the verdict drive_check must reach.

    sudo python3 test_drive_check.py

Linux only (it formats images with parted, mkfs.ext4 and debugfs, as root for
the loop devices). Every image is created in a temporary folder and deleted.
Each case states what a real drive in that shape would mean to its user.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drive_check  # noqa: E402

FAILURES: list[str] = []


def run(*cmd: str) -> str:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True).stdout.strip()


def make_drive(path: Path, *, table: str = "gpt", fs_type: str = "ext4", features: str = "",
               label: str = "EXT4TEST", sector: int = 512) -> str:
    """As docs/0a-drive-format.md: one partition from 1 MiB, formatted ext4.
    Returns the filesystem UUID as blkid reads it."""
    path.unlink(missing_ok=True)
    run("truncate", "-s", "256M", str(path))
    if table == "none":
        run("mkfs.ext4", "-q", "-F", "-L", label, *(["-O", features] if features else []), str(path))
        return run("blkid", "-s", "UUID", "-o", "value", str(path))
    loop = run("losetup", "-fP", "--show", "--sector-size", str(sector), str(path))
    try:
        run("parted", "-s", "--", loop, "mklabel", table, "mkpart", "primary", fs_type, "1MiB", "100%")
        run("partprobe", loop) if Path("/usr/sbin/partprobe").exists() else None
        partition = loop + "p1"
        run("mkfs.ext4", "-q", "-F", "-L", label, *(["-O", features] if features else []), partition)
        return run("blkid", "-s", "UUID", "-o", "value", partition)
    finally:
        run("losetup", "-d", loop)


def expect(name: str, report: dict, verdict: str, *, reason: str = "", note: str = "", uuid: str = "") -> None:
    problems = []
    if report.get("verdict") != verdict:
        problems.append(f"verdict {report.get('verdict')!r}, wanted {verdict!r}")
    if reason and not any(reason in r for r in report.get("reasons", [])):
        problems.append(f"no reason mentioning {reason!r}")
    if note and not any(note in n for n in report.get("notes", [])):
        problems.append(f"no note mentioning {note!r}")
    if uuid and report.get("identity") != uuid:
        problems.append(f"identity {report.get('identity')!r}, wanted {uuid}")
    status = "ok  " if not problems else "FAIL"
    print(f"  {status} {name}: {report.get('verdict')}"
          + (f" — {report['reasons'][0][:110]}" if report.get("reasons") else ""))
    if problems:
        FAILURES.append(f"{name}: " + "; ".join(problems) + "\n" + json.dumps(report, indent=2)[:1500])


def main() -> int:
    if os.geteuid() != 0:
        print("run as root: the images are partitioned through loop devices", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="drive-check-") as temporary:
        image = Path(temporary) / "drive.img"

        print("drives that must be accepted")
        uuid = make_drive(image)
        expect("made as documented (newest defaults)", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, features="^orphan_file,^metadata_csum_seed")
        expect("an older drive, before orphan_file", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, features="^orphan_file,^metadata_csum_seed,^metadata_csum,^64bit")
        expect("an old drive, before metadata checksums", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, sector=4096)
        expect("a disk with 4096-byte sectors", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, table="msdos")
        expect("an MBR disk with a Linux partition", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, label="MYDRIVE")
        expect("a different label", drive_check.check(str(image)), "ok", uuid=uuid)

        uuid = make_drive(image, table="none")
        expect("no partition table", drive_check.check(str(image)), "ok", uuid=uuid, note="may offer to format")

        uuid = make_drive(image)
        loop = run("losetup", "-fP", "--show", str(image))
        try:
            run("debugfs", "-w", "-R", "feature needs_recovery", loop + "p1")
        finally:
            run("losetup", "-d", loop)
        expect("pulled out mid-write (journal to replay)", drive_check.check(str(image)), "ok",
               uuid=uuid, note="journal will be replayed")

        print("drives that must be refused")
        for feature, word in (("casefold", "casefold"), ("encrypt", "encrypt"), ("inline_data", "inline_data")):
            make_drive(image, features=feature)
            expect(f"uses {feature}", drive_check.check(str(image)), "refuse", reason=word)

        make_drive(image, fs_type="ntfs")
        expect("partition marked as Windows data", drive_check.check(str(image)), "refuse",
               reason="Windows treats it as its own")

        make_drive(image)
        with open(image, "r+b") as stream:   # one byte of the label, checksum left stale
            stream.seek(1024 * 1024 + 1024 + 0x78)
            stream.write(b"X")
        expect("superblock checksum damaged", drive_check.check(str(image)), "refuse", reason="checksum")

        uuid = make_drive(image)
        loop = run("losetup", "-fP", "--show", str(image))
        try:
            run("debugfs", "-w", "-R", "ssv state 3", loop + "p1")
        finally:
            run("losetup", "-d", loop)
        expect("marked as having errors", drive_check.check(str(image)), "refuse", reason="errors")

        run("truncate", "-s", "0", str(image))
        run("truncate", "-s", "64M", str(image))
        expect("an empty disk", drive_check.check(str(image)), "refuse", reason="no ext4")

        print("never writes")
        make_drive(image)
        before = image.read_bytes()
        drive_check.check(str(image))
        if image.read_bytes() != before:
            FAILURES.append("drive_check changed the image it read")
        print(f"  {'ok  ' if image.read_bytes() == before else 'FAIL'} the image is byte-identical after a check")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):", file=sys.stderr)
        for failure in FAILURES:
            print(failure, file=sys.stderr)
        return 1
    print("\ndrive_check: ok — accepts every drive shape it was qualified for, refuses what it has not been "
          "qualified for, and reads without writing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
