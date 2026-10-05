# 0c step 1 — does "save now" reach the disk? (passed, after three fixes)

Measured 2026-10-03 on Windows 11 Pro x64, LKL `d0f76a77e` (kernel 6.12),
built natively in MSYS2 with gcc 15.3.0. Source: `flushproof/flushproof.c`.
Patch: `patches/apply_virtio_blk_flush.py`, recorded as `patches/lkl-flush.diff`.

## Why this had to be step 1

Everything in this project rests on one thing. ext4 keeps a pulled drive whole
by ordering its journal with flushes, and PostgreSQL and SQLite make a commit
durable with fsync, which becomes the same flush. If a flush does not reach
Windows' `FlushFileBuffers`, the data can still be sitting in Windows' own
cache when the drive is pulled, while every program believes it is saved. No
amount of later testing would find that reliably: it would show up as rare,
unexplained loss on an owner's drive.

## Result

| Build | writes reaching the host | flushes reaching the host |
|---|---|---|
| LKL as shipped | 23 | **0** |
| feature bit set, LKL unpatched | 23 | 0, plus 5 × `virtio_blk: no status buf` |
| **feature bit set, LKL patched** | 23 | **5** (1 mount, 2 fsync, 2 sync) |

The counts come from a wrapper around LKL's own block-device operations that
tallies each request and then calls through to the real handler, so a counted
flush is one that reached `FlushFileBuffers`.

## The three faults, each of which alone loses data

1. **The disk said it had no write cache.** `lkl_disk_add` sets
   `device_features = 0`, so `VIRTIO_BLK_F_FLUSH` is absent, and a guest told
   the disk has no cache never sends a flush at all. Our program sets that bit
   on the disk it adds; LKL needs no change for this.
2. **The host rejected every flush.** A flush carries no data, so the guest
   sends two descriptors (header and status byte), and `blk_enqueue` demanded
   three — "no status buf". The arithmetic below that guard already handled two
   correctly; only the guard was wrong. The patch also adds the data-descriptor
   check that the old guard was doing by accident, so a malformed read or write
   is still refused.
3. **A failed flush was reported as success.** `nt-host.c` sets `err = 1` when
   `FlushFileBuffers` fails, and the only test is `if (err < 0)`. So Windows
   could say "I could not make this durable" and the guest would be told it
   was. This is worse than no flush: ext4 would commit its journal believing
   earlier writes had landed. Now it reports the failure.

Faults 2 and 3 have stood in LKL unnoticed because nothing exercised them:
without fault 1 fixed, no flush is ever sent, so neither can fire.

## What this does not yet prove

- That a flush is **enough**. A USB drive's own cache can still hold data after
  `FlushFileBuffers` returns. Step 3 opens the device write-through and
  unbuffered, and the spare test USB drive in step 7 is what answers this for
  real hardware.
- That the data survives a pull. That is the 0b harness, from step 3 onward.
- Anything about `umount`: it showed 0 flushes here because the earlier `sync`
  had already left nothing dirty.

## Build notes, so this can be repeated

- **Build on Windows in MSYS2** (the `MSYS` environment, not `MINGW64`), which
  is the only route LKL's own CI tests. Installed at `<path>`,
  no administrator rights, and `pacman -S base-devel gcc git bc python-pip
  dosfstools` as its CI does.
- **Do not cross-compile from Linux.** Both routes were tried and both fail:
  - **x86_64 mingw** cannot build the kernel at all. Windows makes `long` 32
    bits where Linux assumes 64, so the kernel's own consistency checks fail
    (`offsetof(struct page, …)` assertions).
  - **i686 mingw** compiles and links, but crashes inside `lkl_start_kernel`,
    even using LKL's bundled 2015 patched binutils. LKL's documentation still
    describes this route; its CI no longer tests it.
- **Build the test inside LKL's own test tree** (`tools/lkl/tests/`, with an
  entry in `Targets` and `tests/Build`). Linking the kernel into a program by
  hand fails with "relocation truncated to fit": the kernel is ~220 MB and
  needs the image-base and auto-import link settings LKL's build supplies.
- `make -j` breaks LKL's header install (`headers_install.py` mis-reads `-j`),
  so build without it.
- Two headers need a shim when cross-compiling (`Windows.h`, `Winreg.h`); not
  needed in MSYS2.
