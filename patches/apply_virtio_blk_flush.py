#!/usr/bin/env python3
"""Let a flush reach the disk, and make a failed one say so.

    python3 apply_virtio_blk_flush.py <lkl checkout>

Three things stood between an fsync inside LKL and the data being on the disk,
and all three have to be right before an ext4 drive on Windows can be pulled
out safely:

  1. LKL's virtual disk advertises no write cache (device_features = 0, so no
     VIRTIO_BLK_F_FLUSH). A guest told the disk has no cache never sends a
     flush at all. Our program sets that bit on the disk it adds; nothing in
     LKL needs changing for it.
  2. Even with the bit set, the host rejected every flush: a flush carries no
     data, so the guest sends two descriptors (header and status), and
     blk_enqueue demanded three -- "virtio_blk: no status buf".
  3. A flush that FAILED was reported as success. nt-host's blk_request sets
     `err = 1` when FlushFileBuffers fails, and the only test is `err < 0`, so
     the guest was told the data was durable when Windows had just said it was
     not. That is worse than no flush at all: ext4 would carry on committing
     its journal in the belief that earlier writes had landed.

This patches 2 and 3. Idempotent, and it refuses rather than guesses if the
code it expects has moved.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

GUARD_COMMENT = """\t/*
\t * A flush carries no data, so the guest sends two descriptors: the
\t * header and the status byte. Demanding three dropped every flush, and
\t * with it ext4's journal barriers and every fsync. Read and write still
\t * need their data descriptor, checked below once the type is known.
\t */
"""
DATA_CHECK = """
\tif (req->buf_count < 3 && lkl_req.type != LKL_DEV_BLK_TYPE_FLUSH &&
\t    lkl_req.type != LKL_DEV_BLK_TYPE_FLUSH_OUT) {
\t\tlkl_printf("virtio_blk: no data buf\\n");
\t\tgoto out;
\t}
"""
FLUSH_FAILURE = """\t\tret = FlushFileBuffers(disk.handle);
\t\t/*
\t\t * -1, not 1: the only test below is `err < 0`, so a failed flush
\t\t * was reported to the guest as success, and ext4 would commit its
\t\t * journal believing earlier writes had landed.
\t\t */
\t\tif (!ret)
\t\t\terr = -1;
"""


def patch_virtio_blk(source: Path) -> bool:
    text = source.read_text()
    if "no data buf" in text:
        print("virtio_blk.c: already patched")
        return True

    guard = re.search(r"[ \t]*if \(req->buf_count < 3\) \{\n[ \t]*lkl_printf\("
                      r"\"virtio_blk: no status buf\\n\"\);\n[ \t]*goto out;\n[ \t]*\}\n", text)
    if not guard:
        print("virtio_blk.c: the buf_count guard is not where this patch expects it; "
              "read blk_enqueue and update this patch", file=sys.stderr)
        return False
    text = text[:guard.start()] + GUARD_COMMENT + guard.group(0).replace("< 3", "< 2") + text[guard.end():]

    call = re.search(r"\n[ \t]*t->status = blk_dev->ops->request\(blk_dev->disk, &lkl_req\);\n", text)
    if not call:
        print("virtio_blk.c: the request call is not where this patch expects it", file=sys.stderr)
        return False
    text = text[:call.start()] + DATA_CHECK + call.group(0) + text[call.end():]

    source.write_text(text)
    print("virtio_blk.c: patched -- a flush now reaches the host")
    return True


def patch_nt_host(source: Path) -> bool:
    text = source.read_text()
    if "-1, not 1" in text:
        print("nt-host.c: already patched")
        return True

    failure = re.search(r"[ \t]*ret = FlushFileBuffers\(disk\.handle\);\n"
                        r"[ \t]*if \(!ret\)\n[ \t]*err = 1;\n", text)
    if not failure:
        print("nt-host.c: the flush failure path is not where this patch expects it; "
              "check whether a failed FlushFileBuffers still reports success", file=sys.stderr)
        return False
    text = text[:failure.start()] + FLUSH_FAILURE + text[failure.end():]
    source.write_text(text)
    print("nt-host.c: patched -- a failed flush is now reported as a failure")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.split("\n")[0], file=sys.stderr)
        return 2
    tree = Path(sys.argv[1])
    ok = patch_virtio_blk(tree / "tools/lkl/lib/virtio_blk.c")
    ok = patch_nt_host(tree / "tools/lkl/lib/nt-host.c") and ok
    if not ok:
        return 1
    diff = subprocess.run(["git", "-C", str(tree), "diff", "--",
                           "tools/lkl/lib/virtio_blk.c", "tools/lkl/lib/nt-host.c"],
                          capture_output=True, text=True)
    (Path(__file__).parent / "lkl-flush.diff").write_text(diff.stdout, newline="\n")
    print(f"diff recorded: lkl-flush.diff ({len(diff.stdout.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
