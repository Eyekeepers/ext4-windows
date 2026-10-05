# 0c step 7 — a real USB drive, pulled for real

Step 3 proved writing and recovery on an image file. This step repeats it on a
real device, because an image cannot answer two questions: whether the *device*
keeps what it said it kept when its power is cut, and whether anything in the
chain between Windows and the drive holds data of its own.

The drive is a spare 29.3 GB flash stick, serial `<serial>`, formatted
2026-10-05 as `tools/prepare_drive.sh` formats a drive. **No other drive is
used at any point.** The harness finds the drive by that serial every time and
refuses to run against anything else, so a wrong disk number cannot send it at
the wrong drive.

## The finding so far: SQLite rolled back a commit it had acknowledged

First pass, one pull, 56 acknowledged writes. The filesystem came back clean —
journal replayed, `e2fsck -fn` clean — and one acknowledged row was gone from
the `sqlite/rollback` database (`docs/results/windows-usb-sqlite-loss-2026-10-05.jsonl`).

The cause is the same shape as the rename fault step 3 found, in the one place
step 3's image run never happened to hit:

- SQLite in rollback-journal mode **commits by deleting its journal file**. The
  commit is the deletion, not a write.
- With `synchronous=EXTRA` it then asks for the *folder* to be made durable, so
  that the deletion survives a power cut. SQLite's own documentation says FULL
  is not enough in this mode for exactly this reason.
- On Windows a program cannot fsync a folder, so SQLite's Windows code never
  makes that request — there is nothing for it to call.
- The pull therefore lost the deletion, the journal came back on recovery, and
  SQLite did what a journal present at startup means: it rolled the transaction
  back. The database was not corrupt. It was correctly older than what it had
  told the program was committed.

**Fix:** `ext4win` now makes every change to a folder's contents durable before
reporting success — delete, remove folder, create folder, and rename as before
— in one helper (`sync_parent`). That gives Windows programs the guarantee a
careful Linux program gives itself, since on Windows they cannot ask for it.
It costs one journal commit per such change, which a single owner's agent never
notices.

This is a finding for the product's Windows durability inventory (2a), not only
for this program: **on Windows, any database that commits by removing a file is
exposed unless the filesystem closes the gap.** The engines' own SQLite
settings are inventoried in `docs/WINDOWS-DURABILITY.md` on the the product side.

Reformatted after the fix for a clean start; the ten-pull rerun is pending.

## What this step still owes

- Ten automated kill-pulls on the stick, clean.
- Manual cable pulls, the walkthrough below. Only a cable pull cuts the
  device's own power, so only a cable pull tests the stick's own cache.
- Drive-letter changes and round trips to Linux between pulls.
- A soak run (step 8).

## Walkthrough: running the pull tests yourself on Windows

Everything below runs on this PC. Nothing touches a other drive, and nothing
needs the internet.

### What has to be true first

- The spare stick is plugged in. **Check its serial, not its drive number:**
  drive numbers move between plugs.
- Dokany is installed (it is), and WSL's Ubuntu is installed (it is).
- `usbipd` is installed (it is), so Linux can judge the real device.

### 0. Which disk is the stick

In any PowerShell window:

```powershell
Get-Disk | Select-Object Number, FriendlyName, SerialNumber, Size, BusType
```

The stick is the one whose `SerialNumber` is `<serial>`. A 238.5 GB
`another USB drive` in that list is a **other drive** — leave it alone; the
harness will refuse it anyway.

### 1. Hand the stick to Linux, so Linux can format and judge it

usbipd needs a WSL window open. Leave this running in its own window:

```powershell
wsl -d Ubuntu -e sleep 7200
```

Then, in an **administrator** PowerShell (one time per boot):

```powershell
& 'C:\Program Files\usbipd-win\usbipd.exe' bind --busid 3-2
```

and in any PowerShell:

```powershell
& 'C:\Program Files\usbipd-win\usbipd.exe' attach --wsl --busid 3-2
wsl -d Ubuntu -u root -e lsblk -o NAME,SIZE,FSTYPE,LABEL,SERIAL,TRAN
```

The stick should appear as a ~29.3 GB device with `TRAN=usb` and its serial.
`3-2` is its USB port: if it is plugged in somewhere else, find the new id with
`usbipd list` and use that. The harness checks the id belongs to this serial
before it starts.

### 2. Format it (only when starting fresh)

This erases the stick. It is the same two commands `prepare_drive.sh` runs, and
the script refuses any device that is not USB, under 40 GB and that serial:

```powershell
wsl -d Ubuntu -u root -e bash /tmp/fs.sh
```

(`format-stick.sh` in the session scratchpad is the source; it is copied to
`/tmp/fs.sh` with Windows line endings stripped.)

### 3. Automated pulls — ten of them, unattended

In an **administrator** PowerShell, because writing a raw disk requires it:

```powershell
cd <path>
<path> -u `
  -m yank.drivers.windows_usb `
  --program <path>\ext4win.exe --serial <serial> --busid 3-2 `
  --work <somewhere on C:> --pull kill --iterations 10
```

Each iteration brings the stick back to Windows, mounts it through `ext4win` at
`N:`, writes hard into a new folder, kills `ext4win` at a random moment between
3 and 12 seconds, hands the stick to Linux, and has Linux replay, `e2fsck -fn`
and re-check **every** acknowledged write of **every** run so far. One line per
iteration, `PASS` or `FAIL` with what was lost.

Expect about a minute per iteration. The work folder keeps `results.jsonl` and
one `ext4win-N.log` per run.

### 4. Manual pulls — the part only you can do

Same command with `--pull manual --iterations 3`. It prints, when the writing
is at full speed:

```
>>> PULL THE DRIVE OUT NOW (the cable, not Eject). Waiting...
```

**Pull the stick out of the port.** Do not use Eject, and do not use "Safely
remove hardware": those flush the device first, which is the opposite of what
is being tested. The harness notices the device is gone, then says:

```
pulled. >>> Count to five, then PLUG IT BACK IN. Waiting...
```

Plug it back into **the same port**. Linux then judges it exactly as above.

Three pulls is enough to be meaningful; more is better. If one fails, the
output names the pattern and the first lost record, and `results.jsonl` keeps
the whole verdict.

### If something goes wrong

- **"no USB device with serial … is known to this computer"** — it is not
  plugged in, or it is attached to WSL and Windows cannot see it either. Run
  `usbipd list`.
- **"bus 3-2 is not …"** — it is in a different port. `usbipd list` gives the
  right `--busid`.
- **"WSL did not see the drive; is a WSL window open?"** — the `wsl … sleep`
  window from step 1 closed. usbipd cannot attach to a WSL that is not running.
- **"ext4win exited while mounting"** — read the `ext4win-N.log` in the work
  folder. "cannot open … error 5" means the PowerShell is not elevated.
- **A drive letter is stuck after a crash** — nothing is mounted; Dokany
  releases it when the process dies. `N:` free again is the test.

### What the result means

A `PASS` line says: the filesystem came back clean, and every write the drive
had acknowledged before the pull was still there afterwards, in that run and
every earlier one. A `FAIL` line names what was lost. Writes that were never
flushed may be lost and that is not a failure — it is what Linux does too, and
the harness only ever checks what was acknowledged.
