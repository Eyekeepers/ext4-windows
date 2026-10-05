# 0c step 2 — an ext4 drive as a Windows folder, read-only (passed)

Measured 2026-10-03 on Windows 11 Pro x64, with **Memory Integrity (HVCI)
running**. Program: `src/ext4win.c`. Dokany 2.3.1.1000, LKL `d0f76a77e`
(kernel 6.12) with `patches/lkl-flush.diff`, built in MSYS2.

## What was asked of this step

Mount a disposable ext4 image read-only, browse it from Windows, and do not
hang when the drive vanishes.

## Result: both halves pass

**Windows reads the drive, byte for byte.** An ext4 image made in WSL with the
shapes an agent's drive has was mounted at `M:` and listed by Windows:

| Checked | Result |
|---|---|
| Directory listing | `app`, `data`, `readme.txt`, `lost+found`, all marked read-only |
| Nested directories | `data/hermes/profiles/personal`, `data/secrets` |
| File contents | `readme.txt` read correctly through Windows |
| **A 16-byte file, SHA-256** | `8CE9A80…79DEE` — identical to Linux's |
| **A 3 MB random file, SHA-256** | `CF01BDB…F7736` — identical to Linux's |
| A non-ASCII name | `naïve-café.md` listed correctly |

Two matching checksums, one of them 3 MB of random data, is the real result:
every layer — our path translation, LKL's ext4, the patched block device, the
Windows handle — returns exactly the bytes Linux wrote.

**Nothing hangs when the drive goes.** With a read running in a loop, the
program was killed outright, which is what a vanished drive looks like from
Windows' side:

- `M:` disappeared at once;
- listing it afterwards failed in **0.02 seconds** with "drive not found",
  rather than the long block a filesystem driver can cause;
- no mount point was left behind.

## Three findings worth keeping

1. **Dokany 2.x silently does nothing without `DokanInit()`.** `DokanMain`
   returned `DOKAN_SUCCESS` and the program exited immediately, with no mount
   and no error. An hour went into that. `DokanInit()` before and
   `DokanShutdown()` after are required, and the program now also names each
   failure code in plain words.
2. **Windows' volume list does not show the mount.** Files work, but
   `Get-Volume -DriveLetter M` returns nothing, because without
   `DOKAN_OPTION_MOUNT_MANAGER` the mount is not registered with Windows'
   mount manager. To settle before Project 7, which has to find the drive:
   whether the product needs it listed as a volume, and whether the mount-manager
   option needs administrator rights. Our own code looks the drive up by its
   ext4 UUID and does not need the volume list.
3. **The program carries the MSYS runtime.** It depends on `msys-2.0.dll`
   besides `dokan2.dll` and the Windows libraries, because LKL needs a POSIX
   environment to build. That is one more file to ship, and its licence
   (Cygwin's, LGPLv3) has to travel with it. Decide in step 9 whether to ship
   it or to attempt a `MINGW64` build with no POSIX runtime.

## Settings this step fixed

- `WRITE_PROTECT` while read-only, so Windows itself refuses writes rather
  than relying on our code to.
- `REMOVABLE`, so Windows treats the drive as it treats any stick.
- `CURRENT_SESSION`, so the mount belongs to the session that started it and is
  not offered to other accounts. This is the beginning of the per-user
  isolation step 4 has to finish.
- `CASE_SENSITIVE`, because ext4 is.
- Single-threaded, because LKL is one kernel and this program is its only
  caller. Revisit only with measurements.
- The device is opened `FILE_FLAG_WRITE_THROUGH | FILE_FLAG_NO_BUFFERING`, so
  Windows holds no cache of its own behind our flushes.
- `commit=1` when read/write, so at most about a second of unsynced data is at
  risk on a pull, rather than ext4's default five.

## Next

Step 3: read/write, with the 0b yank harness running on it. That is where
`--read-write` and the flush path from step 1 are measured together, and where
the real question is answered: pull the drive and lose nothing that was saved.
