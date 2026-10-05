# ext4 for Windows, safe to pull out

Working name. The public name and home are not chosen yet; nothing here is
published.

A Windows program that reads and writes an ext4 drive using the Linux kernel's
own ext4 code (LKL), presented to Windows through the free, project-signed
Dokany driver. It exists so that a drive — ext4, because ext4 keeps
credentials private with real ownership and modes — can be used on Windows
without buying a driver, and so that pulling the drive out without ejecting it
is safe.

"Safe" is the whole requirement and it is measured, not asserted:

- the filesystem is never damaged by a pull;
- anything already saved is still there after it;
- plugging the drive back in recovers by itself.

## Layout

| Path | What |
|---|---|
| `docs/0a-drive-format.md` | what a drive's ext4 looks like, so the program supports exactly that |
| `docs/0c-step1-flush-proof.md` | the measurement that "save now" reaches the disk, and the three faults found doing it |
| `docs/0d-no-redis.md` | why an internal note, since removed |
| `docs/results/` | recorded measurements, kept as evidence |
| `harness/` | the yank test harness (Phase 0b): a realistic write workload, a log of every write the moment it is acknowledged, ways to pull the disk, and a verifier that checks every acknowledged write survived |
| `preflight/` | the read-only check that decides whether a disk is one we may open, and its tests |
| `patches/` | the changes LKL needs, as a script that refuses rather than guesses, plus the recorded diff |
| `flushproof/` | the step-1 program: counts the writes and flushes that reach Windows |

## Building

The Windows side is built **on Windows, in MSYS2** (its `MSYS` environment),
which is the only route LKL's own CI tests. Cross-compiling from Linux does not
work; `docs/0c-step1-flush-proof.md` records both attempts and why.

The harness is built before the filesystem program, and Linux's own kernel
driver is measured with it first. That gives the bar the Windows program must
meet, and proves the harness itself catches loss.

## Licence

To be GPL-2.0, matching LKL, before anything is published.
