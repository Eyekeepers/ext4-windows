# 0c step 8 — the long run

Step 8 asks for hours of the workload with random pulls and zero failures of
the pass criteria.

Measured 2026-10-05, Windows 11 Pro x64, `ext4win --read-write` on a 3 GiB image
laid out as a real drive is (GPT, one partition; `0a-drive-format.md`), pulled by
killing the program at a random moment 2–20 s into heavy writing.

## Result: 50 of 50 pulls passed

- **100,134 writes acknowledged** across the fifty runs.
- **81,941 re-checked on the final pass** — every run's acknowledged writes are
  verified again after *every* later pull, so an early run's data has to
  survive all forty-nine pulls that follow it.
- **None lost, none torn.** `e2fsck -fn` clean after every pull.

What was checked on the last pass:

| Pattern | Records |
|---|---|
| fsync'd appends | 27,101 |
| SQLite rollback journal (`synchronous=EXTRA`) | 27,190 |
| SQLite WAL (`synchronous=FULL`) | 27,150 |
| 8 MB copies | 300 |
| credential files | 50 |
| atomic replaces (fsync'd, app-style, and no-fsync) | 150 |

Results: `docs/results/windows-soak-50-2026-10-05.jsonl`.

**The rollback-journal column is the point of this run.** That is the pattern
that lost an acknowledged commit on the real drive before the folder-durability
fix (`0c-step7-usb-drive.md`), and 27,190 of its rows now survive fifty pulls.
It is also the mode some real software falls back to on this filesystem
(`0c-step4-ownership-and-paths.md`), which is why it was worth proving at this
volume rather than at ten pulls.

PostgreSQL is not in this run: it needs a Windows build of PostgreSQL run from
the drive, which does not exist yet.

## What this does not answer

This is an image, so it is a disk with no write cache of its own: a write the
disk reported as complete is kept, because the bytes are on the host's NVMe.
The question of whether a *device* keeps what it acknowledged when its power is
cut belongs to the spare USB stick and a physical cable pull — step 7.

Nor is it hours. Fifty pulls took about forty minutes, and each iteration costs
more than the last because every earlier run is re-verified. A longer run is
cheap to start and worth doing on the stick once the automated runs there are
unattended.
