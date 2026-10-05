# Yank harness (Phase 0b)

Pull the disk out mid-write, then prove nothing that was saved was lost.

Python 3.11+ standard library only, so the same workload and verifier run on
Linux and on Windows. The disk-pulling half is per platform (`yank/drivers/`).

## What it writes

The patterns software on a drive actually uses, each with exactly the
durability that software asks for (see `yank/workload.py`):

| Pattern | Durability asked for | What must hold after a pull |
|---|---|---|
| credential file | fsync, plus its folder | intact |
| replace/durable | temp, fsync, rename, fsync folder | never older than the last acknowledged version |
| replace/app (the product's `integration.write_text`) | temp, fsync, rename | a whole version, never torn |
| replace/none (`seed.py`'s `_write`, most Node code) | temp, rename, no fsync | a whole version, never empty or torn |
| append to memory | fsync per line | every acknowledged line, in order |
| SQLite, `DELETE` journal | `synchronous=EXTRA` | every acknowledged row; `integrity_check` ok |
| SQLite, `WAL` | `synchronous=FULL` | every acknowledged row; `integrity_check` ok |
| large file copy | fsync, plus its folder | acknowledged copies byte-identical |
| PostgreSQL 16 (the product's own build) | default commit | every acknowledged commit; index and table agree; `pg_checksums` clean |

Every write is recorded in an ack log on another disk the moment it is
acknowledged. The verifier checks the recovered drive against that record, and
also checks every earlier run on the same drive.

## Linux reference

```
sudo python3 -m yank.drivers.linux_dm --work /var/tmp/yank --iterations 20 \
     --pg-payload <product>/dist/app/postgres.tar.gz
```

Run from this `harness/` folder. Root attaches the disk; the workload and the
verifier run as an ordinary account. The drive is an image formatted exactly as
`prepare_drive.sh` formats a real one; the pull swaps its device-mapper table
for the `error` target with `--noflush`. After each pull it mounts the drive
(the journal replays), unmounts, runs `e2fsck -fn`, mounts again and verifies.

`--self-test` runs one iteration on a disk that silently drops writes it has
acknowledged, for the two seconds before the pull. It passes only if the
harness catches the loss. If it ever passes the lying disk, nothing else this
harness reports means anything.

## Results so far

- **Self-test, 2026-10-02:** three seeds, each caught. Loss was detected in
  every category: a durable file gone back to an older version, acknowledged
  memory lines missing, rows missing from both SQLite modes, and PostgreSQL
  commits missing.
- **First reference run, 2026-10-02:** the second pull lost one row from the
  `DELETE`-journal SQLite database, the very last write acknowledged before the
  pull, while it used `synchronous=FULL`. SQLite documents that in this mode
  only `EXTRA` makes the last commit durable, so the harness was asking more
  than the setting promises. It now uses `EXTRA`. The finding goes to
  the product's Project 2, which checks which modes the engines' databases use.
- **Reference run, 2026-10-02 (the bar Windows must meet):** 20 pulls at
  random moments, 3.2–14.5 s into heavy writing, on one 2 GiB drive image.
  Results are in `docs/results/linux-reference-2026-10-02.jsonl`.
  - **Every pull passed.** The drive mounted, the journal replayed and
    `e2fsck -fn` was clean every time.
  - **26,793 writes were acknowledged, and none was lost.** The final pass
    re-checked every earlier run: 23,070 checks covering 5,803 PostgreSQL
    commits (page checksums clean), 5,730 and 5,597 SQLite rows, 5,740 memory
    lines, 120 file copies and 20 credential files.
  - **Even the weakest pattern held.** Files replaced with no fsync at all,
    as most Node code and `seed.py` write them, were never empty, torn or
    rolled back, because ext4's `auto_da_alloc` protects that pattern. The
    Windows program must give the same.
