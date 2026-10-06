# 0c step 4 — who the files belong to, and what Windows can learn about them

Step 4 was planned as one question: do the drive's files appear owned by the
owner's Windows account and nobody else, which is the Windows equivalent of
`chmod 700` on the credentials. Measuring it turned up a second question that
turned out to matter more, so both are here.

Measured 2026-10-05, Windows 11 Pro x64, Memory Integrity on.

## The path question, and why it is load-bearing

**A program cannot ask Windows for the real path of a file on our mount.**

`GetFinalPathNameByHandle` fails. Measured through two callers:

| Probe | On a ext4win mount | On NTFS |
|---|---|---|
| Node `fs.realpathSync.native()` | `ENOENT` | the path |
| Python `nt._getfinalpathname()` | `ERROR_FILE_NOT_FOUND` | the path |
| Python `os.path.realpath()` (does not use that API) | the path | the path |
| `fs.statfsSync()` | succeeds, type 0 | succeeds, type 0 |

The Dokany trace shows the request arriving and being answered: Create
succeeds, `GetFileInfo` answers `FileStandardInformation` with success, then
Cleanup and Close. Nothing we are asked for is refused, so the failure is
Windows being unable to turn the volume into a DOS path rather than our
callbacks returning an error.

`DOKAN_OPTION_MOUNT_MANAGER` was tried in place of
`DOKAN_OPTION_CURRENT_SESSION` and did not fix it; the drive also still did not
appear in `Get-Volume`, which is the same symptom recorded in step 2. The run
was not elevated, and Dokany's mount-manager mode may need that, so this is not
yet a settled answer — it is one tried fix that did not work. The option is
kept behind `--mount-manager` rather than removed.

**Why it matters more than it sounds.** Real software makes decisions from this
call. [OpenClaw](https://github.com/openclaw/openclaw), for one, decides SQLite's journal mode
from exactly this call (`dist/sqlite-wal-*.js`, `resolvePathJournalPolicy`): a
drive-letter path is resolved with the real-path call, and **a failure is
treated as network storage**, which selects `journal_mode=DELETE`. Its
databases then run with `synchronous=NORMAL`, a combination SQLite's own
documentation warns can corrupt a database on power loss.

So on this filesystem, as it stands, such a program picks its least safe mode —
not because of anything about ext4, but because one Windows API cannot describe
the volume. The folder-durability fix from step 7 is what keeps that
survivable.

**Still to do:** find out why the volume has no DOS path and whether an
elevated mount-manager registration fixes it. Until then the drive works, and
one engine is quietly in the wrong mode.

## What does work

SQLite's WAL mode works completely, which is the mode both engines want:

| Checked | Result |
|---|---|
| `PRAGMA journal_mode=WAL` takes | yes |
| `-wal` and `-shm` sidecars created | yes |
| a second connection sees an uncheckpointed commit (shared memory) | yes |
| a writer's transaction excludes a second writer (byte-range locks) | yes |
| a reader is not blocked by a writer | yes |
| `wal_checkpoint(TRUNCATE)` | succeeds |
| `integrity_check` | `ok` |
| `journal_mode=DELETE` with `synchronous=EXTRA` | works |

Shared memory and byte-range locking are the two things WAL needs and that
FUSE-like filesystems usually lack, and it is why programs such as Hermes
Agent carry a fallback to rollback-journal mode at all. Both work here, so a
program that tries WAL keeps it.

## Ownership — not yet measured

The original question is still open. Files are created `0600` and directories
`0700` inside ext4, and the mount uses `DOKAN_OPTION_CURRENT_SESSION` so it is
not offered to other sessions. Neither of those is a measurement:

- ext4 modes are not Windows ACLs, and nothing yet maps one to the other;
- proving another account is *denied* needs another account to try it with,
  which means creating one on this machine.

Pending the owner's say-so on creating a second local account. Note that
`--mount-manager` and `CURRENT_SESSION` are mutually exclusive, so if the path
question is solved by the mount manager, the per-session privacy goes with it
and this step's answer changes. They are one decision, not two.
