#!/usr/bin/env python3
"""Decide, without writing a byte, whether a disk is one we may open.

    python drive_check.py <disk, partition or image>      # one JSON object
    python drive_check.py --identity <...>                # just the ext4 UUID

Reads the partition table (GPT, or MBR on older hand-made drives) and the ext4
superblock, nothing else, through a file opened read-only. On Windows the path
is \\\\.\\PhysicalDriveN and the caller is whatever will mount it; here it is an
image file or a block device.

The verdict is "ok" or "refuse", and a refusal always says why, in words a
person could act on. It refuses before anything is mounted:

  - a partition Windows treats as its own (the "format this disk" risk);
  - an ext4 feature outside the set this program has been qualified for --
    fscrypt encryption, case-folding and inline data among them -- because a
    drive we have not tested must not be written by us on the way to finding
    out;
  - a superblock whose checksum does not match, or one marked as having
    errors, which want a Linux repair first.

It also returns the ext4 UUID, which identifies the drive whatever letter or
disk number Windows gives it, and whether or not its USB bridge passes a serial.

Standard library only, so the service can run it from the computer it is
installed on without anything else.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import uuid

LINUX_GPT_TYPE = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
WINDOWS_GPT_TYPES = {
    uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"): "Windows basic data",
    uuid.UUID("e3c9e316-0b5c-4db8-817d-f92df00215ae"): "Microsoft reserved",
    uuid.UUID("de94bba4-06d1-4d40-a16a-bfd50179d6ac"): "Windows recovery",
    uuid.UUID("5808c8aa-7e8f-42e0-85d2-e1e90434cfb3"): "Windows LDM metadata",
    uuid.UUID("af9b60a0-1431-4f62-bc68-3311714a69ad"): "Windows LDM data",
}
MBR_LINUX = 0x83
EXT4_MAGIC = 0xEF53

COMPAT = {0x1: "dir_prealloc", 0x2: "imagic_inodes", 0x4: "has_journal", 0x8: "ext_attr",
          0x10: "resize_inode", 0x20: "dir_index", 0x40: "lazy_bg", 0x80: "exclude_inode",
          0x100: "exclude_bitmap", 0x200: "sparse_super2", 0x400: "fast_commit",
          0x800: "stable_inodes", 0x1000: "orphan_file"}
INCOMPAT = {0x1: "compression", 0x2: "filetype", 0x4: "needs_recovery", 0x8: "journal_dev",
            0x10: "meta_bg", 0x40: "extent", 0x80: "64bit", 0x100: "mmp", 0x200: "flex_bg",
            0x400: "ea_inode", 0x1000: "dirdata", 0x2000: "metadata_csum_seed", 0x4000: "large_dir",
            0x8000: "inline_data", 0x10000: "encrypt", 0x20000: "casefold"}
RO_COMPAT = {0x1: "sparse_super", 0x2: "large_file", 0x4: "btree_dir", 0x8: "huge_file",
             0x10: "gdt_csum", 0x20: "dir_nlink", 0x40: "extra_isize", 0x80: "has_snapshot",
             0x100: "quota", 0x200: "bigalloc", 0x400: "metadata_csum", 0x800: "replica",
             0x1000: "readonly", 0x2000: "project", 0x4000: "shared_blocks", 0x8000: "verity",
             0x10000: "orphan_present"}

# What a drive formatted as in docs/0a-drive-format.md has, by any e2fsprogs from the last decade
# (docs/0a-drive-format.md). Older drives use a subset; that is fine. Anything
# else has not been through the yank harness, so it is refused rather than
# found out about with someone's data. Widen this only with a test run.
QUALIFIED = {
    "compat": {"has_journal", "ext_attr", "resize_inode", "dir_index", "sparse_super2",
               "orphan_file", "fast_commit", "lazy_bg"},
    "incompat": {"filetype", "extent", "64bit", "flex_bg", "metadata_csum_seed", "meta_bg",
                 # States, not features: the journal or the orphan list has
                 # work to do after a pull. The mount does it.
                 "needs_recovery"},
    "ro_compat": {"sparse_super", "large_file", "huge_file", "gdt_csum", "dir_nlink",
                  "extra_isize", "metadata_csum", "orphan_present"},
}
WHY = {
    "encrypt": "it uses per-file encryption (fscrypt), which this program has not been tested with",
    "casefold": "it has case-insensitive folders, which this program has not been tested with",
    "inline_data": "it stores small files inside their inodes, which this program has not been tested with",
    "journal_dev": "its journal lives on another device",
    "compression": "it asks for compression, which ext4 has never supported",
    "verity": "it has fs-verity files",
    "bigalloc": "it uses bigalloc clusters",
    "mmp": "it is set up for shared storage between computers (mmp)",
    "has_snapshot": "it carries an obsolete snapshot feature",
}

_CRC32C_TABLE: list[int] = []


def crc32c(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    """Castagnoli CRC, as ext4's metadata_csum uses. ext4 keeps the raw value,
    without the final inversion."""
    if not _CRC32C_TABLE:
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
            _CRC32C_TABLE.append(c)
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc


class Reader:
    """Aligned reads only: Windows refuses unaligned reads on a raw disk."""

    ALIGN = 4096

    def __init__(self, path: str) -> None:
        self.stream = open(path, "rb", buffering=0)

    def read(self, offset: int, length: int) -> bytes:
        start = offset - offset % self.ALIGN
        end = offset + length
        end += (-end) % self.ALIGN
        self.stream.seek(start)
        data = self.stream.read(end - start)
        return data[offset - start: offset - start + length]

    def close(self) -> None:
        self.stream.close()


def names(bits: int, table: dict[int, str]) -> list[str]:
    found = [name for bit, name in table.items() if bits & bit]
    unknown = bits & ~sum(table)
    if unknown:
        found.append(f"unknown(0x{unknown:x})")
    return sorted(found)


def partitions(disk: Reader) -> tuple[str, list[dict]]:
    """('gpt'|'mbr'|'none', [{index, type, type_name, offset, size}])."""
    for sector in (512, 4096):
        header = disk.read(sector, 92)
        if header[:8] != b"EFI PART":
            continue
        entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
        table = disk.read(entries_lba * sector, count * entry_size)
        found = []
        for index in range(count):
            entry = table[index * entry_size:(index + 1) * entry_size]
            type_guid = uuid.UUID(bytes_le=entry[:16])
            if type_guid.int == 0:
                continue
            first, last = struct.unpack_from("<QQ", entry, 32)
            name = entry[56:128].decode("utf-16-le", "replace").rstrip("\x00")
            found.append({"index": index + 1, "type": str(type_guid), "name": name,
                          "type_name": "Linux filesystem" if type_guid == LINUX_GPT_TYPE
                          else WINDOWS_GPT_TYPES.get(type_guid, "other"),
                          "offset": first * sector, "size": (last - first + 1) * sector,
                          "sector": sector})
        return "gpt", found
    mbr = disk.read(0, 512)
    if mbr[510:512] == b"\x55\xaa":
        found = []
        for index in range(4):
            entry = mbr[446 + index * 16: 446 + (index + 1) * 16]
            kind = entry[4]
            first, count = struct.unpack_from("<II", entry, 8)
            if kind == 0 or count == 0:
                continue
            if kind == 0xEE:   # protective MBR without a readable GPT
                return "gpt-unreadable", []
            found.append({"index": index + 1, "type": f"0x{kind:02x}",
                          "type_name": "Linux" if kind == MBR_LINUX else "other",
                          "offset": first * 512, "size": count * 512, "sector": 512})
        if found:
            return "mbr", found
    return "none", []


def superblock(disk: Reader, offset: int) -> dict | None:
    raw = disk.read(offset + 1024, 1024)
    if len(raw) < 1024 or struct.unpack_from("<H", raw, 0x38)[0] != EXT4_MAGIC:
        return None
    compat, incompat, ro_compat = struct.unpack_from("<III", raw, 0x5C)
    log_block = struct.unpack_from("<I", raw, 0x18)[0]
    state = struct.unpack_from("<H", raw, 0x3A)[0]
    rev = struct.unpack_from("<I", raw, 0x4C)[0]
    inode_size = struct.unpack_from("<H", raw, 0x58)[0] if rev >= 1 else 128
    info = {
        "uuid": str(uuid.UUID(bytes=raw[0x68:0x78])),
        "label": raw[0x78:0x88].split(b"\x00", 1)[0].decode("utf-8", "replace"),
        "block_size": 1024 << log_block,
        "inode_size": inode_size,
        "features": {"compat": names(compat, COMPAT), "incompat": names(incompat, INCOMPAT),
                     "ro_compat": names(ro_compat, RO_COMPAT)},
        "clean": bool(state & 0x1),
        "errors": bool(state & 0x2),
    }
    if ro_compat & 0x400:   # metadata_csum: the superblock checksums itself
        stored = struct.unpack_from("<I", raw, 0x3FC)[0]
        info["checksum_ok"] = crc32c(raw[:0x3FC]) == stored
    return info


def check(path: str) -> dict:
    disk = Reader(path)
    try:
        scheme, parts = partitions(disk)
        report: dict = {"path": path, "partition_table": scheme, "partitions": parts, "notes": []}
        reasons: list[str] = []

        candidates = [p for p in parts if superblock(disk, p["offset"])]
        if scheme == "none":
            fs = superblock(disk, 0)
            if fs:
                report["notes"].append("ext4 with no partition table: Windows sees this disk as empty "
                                       "and may offer to format it; never accept that offer")
                candidates = [{"index": 0, "type": "none", "type_name": "whole device", "offset": 0}]
        if not candidates:
            reasons.append("no ext4 filesystem was found on this disk")
            return {**report, "verdict": "refuse", "reasons": reasons}
        if len(candidates) > 1:
            reasons.append("more than one ext4 filesystem is on this disk; this program opens one")
            return {**report, "verdict": "refuse", "reasons": reasons}

        part = candidates[0]
        fs = superblock(disk, part["offset"])
        report["selected_partition"] = part["index"]
        report["ext4"] = fs
        report["identity"] = fs["uuid"]

        if part["type_name"] in WINDOWS_GPT_TYPES.values() or (scheme == "mbr" and part["type"] in ("0x07", "0x0b", "0x0c")):
            reasons.append(f"the partition is marked as {part['type_name']}, so Windows treats it as its own "
                           "and may offer to format it; on Linux, mark it as a Linux filesystem first")
        elif scheme == "gpt" and part["type_name"] != "Linux filesystem":
            report["notes"].append(f"partition type {part['type']} is not the usual Linux filesystem type")

        if fs.get("checksum_ok") is False:
            reasons.append("the filesystem's own checksum does not match, so its description of itself "
                           "cannot be trusted; repair it on Linux with e2fsck before using it here")
        if fs["errors"]:
            reasons.append("the filesystem is marked as having errors; repair it on Linux with e2fsck "
                           "before using it here")

        for group, present in fs["features"].items():
            for feature in present:
                if feature not in QUALIFIED[group]:
                    why = WHY.get(feature, "this program has not been tested with it")
                    reasons.append(f"it uses the ext4 feature '{feature}': {why}. It still opens on Linux")

        if "needs_recovery" in fs["features"]["incompat"] or "orphan_present" in fs["features"]["ro_compat"]:
            report["notes"].append("it was not shut down cleanly; its journal will be replayed when it is "
                                   "opened, which is what keeps a pulled drive whole")
        return {**report, "verdict": "refuse" if reasons else "ok", "reasons": reasons}
    finally:
        disk.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path")
    parser.add_argument("--identity", action="store_true", help="print only the drive's ext4 UUID")
    args = parser.parse_args()
    try:
        report = check(args.path)
    except OSError as error:
        print(json.dumps({"verdict": "refuse", "reasons": [f"the disk could not be read: {error}"]}))
        return 2
    if args.identity:
        if "identity" not in report:
            return 1
        print(report["identity"])
        return 0
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
