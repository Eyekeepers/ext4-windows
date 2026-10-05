# ext4-windows

**Read and write Linux ext4 drives on Windows, and pull the drive out without
losing anything.**

`ext4win.exe` mounts an ext4 partition as a Windows drive letter. It is not a
reimplementation of ext4: it runs **the Linux kernel's own ext4 code**, journal
and all, through [LKL](https://github.com/lkl/linux), and presents it to Windows
through the free, Microsoft-signed [Dokany](https://dokan-dev.github.io/) driver.
So there is nothing to buy, and no kernel driver of ours for Windows to trust.

## What makes it different: a pull is not a failure

Removable drives get pulled out. Many devices have no eject button, and people
in a hurry do not use the one on screen. So this program treats a surprise
removal as ordinary, and the requirement is measured rather than asserted:

- **the filesystem is never damaged** by a pull;
- **anything already saved is still there** afterwards;
- **plugging the drive back in recovers by itself.**

It is tested by a harness that logs every write *the moment it is acknowledged*,
pulls the disk at a random moment, and then asks Linux itself — journal replay,
`e2fsck -fn`, and a check of every acknowledged write — whether anything was
lost. Linux's own ext4 driver was measured first, so the bar is a real number
rather than an aspiration.

**Latest: 50 of 50 pulls, 100,134 acknowledged writes, none lost**
([docs/0c-step8-soak.md](docs/0c-step8-soak.md)).

Getting there found three faults that each silently lost data, and they are
worth reading if you are building something similar:

| Fault | Effect |
|---|---|
| LKL advertised no write cache, so the kernel never sent a flush at all | **every "save now" was discarded** ([step 1](docs/0c-step1-flush-proof.md)) |
| A rename was reported as done before it was durable | an acknowledged file replace **reverted to the old version** ([step 3](docs/0c-step3-read-write.md)) |
| A *deletion* was reported as done before it was durable | SQLite in rollback-journal mode **rolled back a commit it had already confirmed** ([step 7](docs/0c-step7-usb-drive.md)) |

The second and third are the same shape, and the shape is worth stating plainly:
**on Windows a program cannot make a change to a folder durable** — there is no
call for it, and a directory handle is not a file. On Linux, `fsync()` on a
directory is how a careful program makes a rename or a delete survive a power
cut. So on Windows that guarantee has to come from the filesystem, and
`ext4win` now provides it inside every rename, delete and folder creation.

## Using it

```
ext4win --disk \\.\PhysicalDrive2 --part 1 --mount N --read-write
```

| Option | |
|---|---|
| `--disk` | `\\.\PhysicalDriveN`, or an image file |
| `--part` | partition number; `0` for a whole-disk filesystem |
| `--mount` | a drive letter |
| `--read-write` | without it, the mount is read-only |
| `--status FILE` | one line of JSON about what the drive is doing, written on the host |
| `--mount-manager` | register the letter with Windows' mount manager instead of the session |
| `--debug` | Dokany's own trace |

**Stopping it:** send `CTRL_BREAK` (or `CTRL_C`). It unmounts cleanly and leaves
nothing for the journal to replay. `dokanctl /u` does **not** work on a
session mount — measured — so this is the way.

**Exit codes**, because a supervisor needs to tell these apart:

| | |
|---|---|
| `0` | unmounted cleanly; this was asked for |
| `1` | could not start or mount |
| `10` | **the drive was taken away** — stop what was using it and wait |

Writing to a physical disk requires administrator rights on Windows. That is
Windows' rule, not ours, and it is why a product using this would install a
privileged helper once rather than asking for rights every time.

Requires the [Dokany](https://github.com/dokan-dev/dokany) driver (installed
separately) and `msys-2.0.dll`, which ships beside the executable.

## Status

Working and measured, **not yet released**. See
[docs/WHERE-WE-ARE.md](docs/WHERE-WE-ARE.md) for what is proven and what is
still open — ownership mapping and re-plug behaviour are the main gaps, and
`GetFinalPathNameByHandle` does not yet work on the mount, which
[matters more than it sounds](docs/0c-step4-ownership-and-paths.md).

## Layout

| Path | What |
|---|---|
| `src/ext4win.c` | the program |
| `build/` | the pinned build (`pins.json`) and the build script |
| `patches/` | the changes LKL needs, as a script that refuses rather than guesses, plus the diff |
| `harness/` | the pull-test harness: workload, acknowledgement log, ways to pull the disk, verifier |
| `harness/probes/` | small measurements kept so they can be re-run after a change |
| `preflight/` | the read-only check deciding whether a disk is one we may open |
| `flushproof/` | the step-1 program: counts the writes and flushes that reach Windows |
| `docs/` | what was measured, and the reasoning. `docs/results/` holds the raw numbers |

## Building

Built **on Windows, in MSYS2** (the `MSYS` environment, not `MINGW64`), which is
the route LKL's own CI tests. Cross-compiling from Linux does not work: x86-64
fails because Windows' `long` is 32 bits, and i686 crashes in
`lkl_start_kernel`. Both attempts are recorded in
[docs/0c-step1-flush-proof.md](docs/0c-step1-flush-proof.md).

```
python patches/apply_virtio_blk_flush.py --tree lkl
bash build/build.sh --lkl lkl
```

Every input is pinned in [build/pins.json](build/pins.json). The GitHub Actions
workflow builds exactly this way, which is what lets a release be checked
against its source.

## Licence

**GPL-2.0** — see [LICENSE](LICENSE). Not a preference: this program links the
Linux kernel's ext4 code, so the combined work carries the kernel's licence.
Components and their terms are in [ATTRIBUTION.md](ATTRIBUTION.md).

Code signing: [CODE-SIGNING-POLICY.md](CODE-SIGNING-POLICY.md).
