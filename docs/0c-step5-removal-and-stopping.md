# 0c step 5 — noticing the drive is gone, and putting it down on purpose

Step 5 asks what happens when the drive is taken away while the program is
using it: does the program notice within seconds, fail open files cleanly
rather than hanging, tell whatever is supervising it, and mark the session
unclean. Writing it turned up a second thing missing that nobody had asked
for — there was no way to stop the drive on purpose at all.

Measured 2026-10-05, Windows 11 Pro x64.

## Stopping on purpose: there was no way to, and now there is

**`dokanctl /u` does not unmount a drive mounted for one session.** Measured
twice: the command returns, and the drive stays. Before this the only way to
stop the program was to kill it — which is the *pull* path. A clean stop and a
power cut were the same operation, which is not a thing a product can ship:
every ordinary shutdown would have been recovered from rather than completed.

`ext4win` now answers a **console control event** — `CTRL_BREAK`, `CTRL_C`, or
Windows closing or shutting it down. That is deliberately the signal Windows
process supervisors already send console programs (`CTRL_BREAK_EVENT` to a
process group), so the drive can be stopped the same way as the programs using
it, rather than needing a mechanism of its own.

Measured end to end:

| | Result |
|---|---|
| `CTRL_BREAK` while mounted | "stopping: unmounting the drive", then exits |
| exit code | 0 |
| drive letter afterwards | gone |
| a file written just before the stop | present |
| `e2fsck -fn` afterwards | **clean with no replay needed** |

That last line is the one that matters: a clean stop leaves nothing for the
journal to replay, which is what distinguishes it from a pull. A close or
shutdown handler also waits for the unmount to finish, because Windows gives it
a few seconds before killing the process and the journal should be settled
inside them.

## Noticing the drive is gone

**Where it is noticed.** Every read, write and flush passes through one
function, so that is the only place that has to look. When a request fails, the
disk is asked its length; if Windows answers with one of its "the device is not
there" errors, the drive is declared gone. This is deliberately not "a request
failed": a bad sector or a busy disk must keep working, and ext4 remounts itself
read-only if its own writes fail. A file-backed image answers that question with
`ERROR_INVALID_FUNCTION`, so an image can never be mistaken for a vanished
drive.

**What happens then**, in order:

1. One plain sentence on stderr.
2. The status file says `removed`.
3. Every further request is answered `STATUS_DEVICE_REMOVED` — Windows' own
   error for this — rather than being passed down to a filesystem whose disk is
   not there. Programs get "the device was removed" instead of a puzzling I/O
   error, and nothing waits on a disk that cannot answer.
4. The drive letter is taken away, from a watcher thread. It has to be another
   thread: `DokanRemoveMountPoint` waits on Dokany's dispatch, which is exactly
   what a callback is holding.
5. The program exits **10**, and `lkl_umount_dev` is skipped — unmounting would
   ask ext4 to write its journal to a disk that cannot take it, and nothing is
   lost by not trying: what was flushed is on the drive, and the journal replays
   on re-plug.

**How anything else finds out.** `--status FILE` writes one line of JSON —
state, mount point, flushes, pid — and the exit code distinguishes the cases:

| Exit | Means | What a supervisor should do |
|---|---|---|
| 0 | unmounted cleanly | nothing; this was asked for |
| 1 | could not start or mount | something is wrong with the drive's software |
| 10 | **the drive was taken away** | stop the agent and wait for it to come back |

The status file is written **on the host, never on the drive**, because at the
moment it matters most the drive is gone.

## What is not yet measured

**The removal path has not been fired on real hardware.** Everything above
about a vanished drive is code and reasoning; the clean-stop half is measured.
Making a device actually vanish needs either the spare USB stick or a VHDX
being dismounted, and both need administrator rights. That is the same approval
step 7 is waiting on, and the two should be run together: the pull test will
exercise detection, the status file and exit 10 as a side effect of what it
already does.

Also still open from step 4: files have ext4 modes and nothing maps them to
Windows ACLs, and proving another account is denied needs a second account.

## One packaging fact confirmed

`ext4win.exe` **will not start** unless `msys-2.0.dll` is beside it or on the
path — it exits `0xC0000135` (DLL not found) before printing anything. It had
only ever been run from a shell that had MSYS2 on the path, so this was
invisible until the program was launched the way a supervisor launches things.
That DLL is Cygwin-derived and LGPLv3, so how it ships is a step 9 decision, not
an afterthought.
