# 0c step 3 — writing from Windows, and pulling the drive (first pass)

Measured 2026-10-05 on Windows 11 Pro x64 with Memory Integrity on.
`ext4win --read-write`, the 0b workload run by Windows' own Python,
`harness/yank/drivers/windows_kill.py`.

## Result: 10 of 10 pulls passed

The drive was pulled at a random moment 3–12 s into heavy writing, ten times on
the same image. "Pulled" means ext4win was killed outright: its disk handle is
write-through and unbuffered, so nothing of ours is left in Windows' cache
afterwards, which from the disk's side is the drive vanishing.

After every pull Linux (WSL) replayed the journal, `e2fsck -fn` was clean, and
the verifier re-checked every acknowledged write of every run so far:
**16,529 writes acknowledged across the ten runs, none lost, none torn.**
Patterns: credential files, durable/app/no-fsync atomic replaces, fsync'd
appends, SQLite in rollback (`synchronous=EXTRA`) and WAL mode, and 8 MB copies.
Results: `docs/results/windows-step3-kill-2026-10-05.jsonl`.

PostgreSQL is not in this run: it needs a Windows build of PostgreSQL run from
the drive, which does not exist yet.

## What it found and fixed on the way

**A rename that was acknowledged came back as the old version.** On Linux a
careful saver fsyncs the folder after the rename; Windows gives programs no way
to do that. ext4win now fsyncs the parent folder inside every rename before
reporting success, so Windows programs get the guarantee careful Linux
programs give themselves. One journal commit per rename.

The same fault in a second place — a *deletion* that a database had counted as
its commit — survived this run and was caught on the real drive in step 7
(`docs/0c-step7-usb-drive.md`). Ten clean image pulls were not enough to find
it; the fix now covers every change to a folder's contents, so these numbers
predate it.

## Behaviour to know

- **What a program was told is saved survives a pull.** An explicit
  `FlushFileBuffers` then an immediate kill: kept.
- **What was never saved may not.** Writes with no flush, killed within about
  two seconds, were lost (the filesystem stayed clean). That is ext4's normal
  behaviour on Linux too, and the pass criteria allow it.
- Write support covers create, overwrite, append, truncate, folders, delete,
  rename (atomic, now durable) and timestamps.

## Still to do in step 3

- Pulls by detaching a VHDX (`Dismount-VHD`) and Hyper-V power-offs, which
  need administrator rights and Hyper-V; the kill test above does not cut a
  real device's power.
- PostgreSQL in the workload, once there is a Windows build to run from the
  drive.
