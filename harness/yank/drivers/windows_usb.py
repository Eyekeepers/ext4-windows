"""0c step 7: the workload on a real USB drive through ext4win, pulled for real.

    python -m yank.drivers.windows_usb --program ext4win.exe --serial SERIAL --busid 3-2
           --work DIR [--pull kill|manual] [--iterations 10] [--letter N]

Run from Windows, in this harness/ folder, in an administrator PowerShell:
writing a physical disk needs it. Nothing here comes from the drive under test;
the harness, ext4win and Python all live on this computer.

The drive is found by its USB serial every time, never by disk number, and the
harness refuses a device that is not on the USB bus given. It must already be
formatted as prepare_drive.sh formats a drive, and it must be a spare: every
iteration writes to it and then takes it away mid-write.

How the drive vanishes (`--pull`):

  kill    (default) ext4win is killed outright mid-write, as in step 3, but on
          real hardware: nothing more reaches the device after that moment.
          The device keeps power, so its own cache survives.
  manual  you pull the cable when told, then plug it back in when told. The
          only pull that also cuts the device's power, and so the only test
          of whether the device itself keeps what it acknowledged.

There is no automated surprise removal. usbipd will not take a device Windows
is using unless it is bound with --force, which takes it from Windows for
good; disabling the device is vetoed while it is open. Both were tried.

Then Linux (WSL, through usbipd) is the judge on the real device, exactly as in
the Linux reference: mount (the journal replays), unmount, `e2fsck -fn` must be
clean, mount again, and the verifier checks every write the log says was
acknowledged -- in this run and every earlier one on the drive. The drive goes
back to Windows for the next iteration.

Iterations number on from what is already in --work, so automated and manual
sessions can share one drive and every earlier run stays checked.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from .. import acklog
from .windows_kill import wsl_path

USBIPD = r"C:\Program Files\usbipd-win\usbipd.exe"
MOUNT = "/mnt/yankusb"

# Finds the drive by serial on the USB bus, so a different disk is never touched.
WSL_JUDGE = r"""
set -u
serial="$1"; shift
dev=""
for d in /sys/block/sd*; do
    n=$(basename "$d")
    [ "$(lsblk -dno SERIAL /dev/$n 2>/dev/null | tr -d ' ')" = "$serial" ] || continue
    [ "$(lsblk -dno TRAN /dev/$n)" = "usb" ] || continue
    dev=/dev/$n
done
[ -n "$dev" ] || { echo "NO_DEVICE"; exit 0; }
part="${dev}1"
for i in $(seq 1 40); do [ -b "$part" ] && break; sleep 0.25; done
mkdir -p MOUNT
umount -l MOUNT 2>/dev/null || true
if ! out=$(mount -t ext4 "$part" MOUNT 2>&1); then echo "MOUNT_FAILED $out"; exit 0; fi
umount MOUNT
if e2fsck -fn "$part" >/tmp/yankusb-fsck.txt 2>&1; then echo FSCK_CLEAN; else echo FSCK_DIRTY; tail -30 /tmp/yankusb-fsck.txt; exit 0; fi
mount -t ext4 "$part" MOUNT
cd "$HARNESS"
python3 -m yank.verify "$@" || true
umount MOUNT
sync
""".replace("MOUNT", MOUNT)


def powershell(command: str) -> str:
    return subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                          capture_output=True, text=True).stdout.strip()


def windows_disk(serial: str) -> int | None:
    number = powershell(f"(Get-Disk | Where-Object {{ $_.SerialNumber -like '*{serial}*' -and "
                        f"$_.BusType -eq 'USB' }}).Number")
    return int(number) if number.isdigit() else None


def usbipd(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run([USBIPD, *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"usbipd {' '.join(args)}: {done.stdout}{done.stderr}".strip())
    return done


def wsl_sees(serial: str) -> bool:
    out = subprocess.run(["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e", "lsblk", "-dno", "SERIAL,TRAN"],
                         capture_output=True, text=True).stdout
    return any(line.split() == [serial, "usb"] for line in out.splitlines())


def check_bus(serial: str, busid: str) -> None:
    """The bus id must still be this drive: ids follow ports, not devices.

    While usbipd holds the device, Windows lists it under usbipd's own stub
    (VID 80EE), so the drive's real identity is the other entry with its serial."""
    out = powershell("Get-PnpDevice -Class USB | Where-Object { $_.InstanceId -like "
                     f"'USB\\VID_*\\{serial}' -and $_.InstanceId -notlike 'USB\\VID_80EE*' }} | "
                     "ForEach-Object { $_.InstanceId } | Select-Object -First 1")
    if not out:
        raise RuntimeError(f"no USB device with serial {serial} is known to this computer")
    vidpid = out.split("\\")[1].replace("VID_", "").replace("&PID_", ":").lower()
    listed = usbipd("list").stdout
    if not any(line.split()[:2] == [busid, vidpid] for line in listed.splitlines() if line.strip()):
        raise RuntimeError(f"bus {busid} is not {vidpid} (serial {serial}); run `usbipd list` and pass --busid")


def to_windows(serial: str, timeout: float = 60) -> int:
    usbipd("detach", "--busid", ARGS.busid, check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        number = windows_disk(serial)
        if number is not None:
            return number
        time.sleep(1)
    raise RuntimeError("the drive did not come back to Windows")


def to_wsl(serial: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wsl_sees(serial):
            return
        usbipd("attach", "--wsl", "--busid", ARGS.busid, check=False)
        time.sleep(2)
    raise RuntimeError("WSL did not see the drive; is a WSL window open?")


def mount(ext4win: Path, number: int, letter: str, log: Path) -> subprocess.Popen:
    stream = open(log, "ab")
    proc = subprocess.Popen([str(ext4win), "--disk", rf"\\.\PhysicalDrive{number}", "--part", "1",
                             "--mount", letter, "--read-write"],
                            stdout=stream, stderr=stream, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    stream.close()
    root = Path(f"{letter}:/")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"ext4win exited while mounting; see {log}")
        if root.exists():
            return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("the drive did not appear")


def next_index(acks: Path) -> int:
    used = [int(p.stem.split("-")[1]) for p in acks.glob("run-*.jsonl")]
    return max(used) + 1 if used else 0


def manual_pull(serial: str) -> None:
    print("  >>> PULL THE DRIVE OUT NOW (the cable, not Eject). Waiting...", flush=True)
    while windows_disk(serial) is not None:
        time.sleep(0.2)
    print("  pulled. >>> Count to five, then PLUG IT BACK IN. Waiting...", flush=True)
    time.sleep(3)
    while windows_disk(serial) is None and not wsl_sees(serial):
        time.sleep(0.5)
    print("  back.", flush=True)


def judge(serial: str, harness: Path, acks: Path, upto: int) -> dict:
    runs = []
    for done in range(upto + 1):
        log = acks / f"run-{done}.jsonl"
        if log.exists():
            runs += ["--run", f"{MOUNT}/run-{done}", wsl_path(log)]
    check = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e", "env", f"HARNESS={wsl_path(harness)}",
         "PYTHONDONTWRITEBYTECODE=1", "bash", "-c", WSL_JUDGE, "judge", serial, *runs],
        capture_output=True, text=True)
    out = check.stdout
    if "NO_DEVICE" in out:
        return {"ok": False, "failure": "WSL could not find the drive"}
    if "MOUNT_FAILED" in out:
        return {"ok": False, "failure": "the drive does not mount after the pull: " + out[-1500:]}
    if "FSCK_CLEAN" not in out:
        return {"ok": False, "failure": "e2fsck -fn found problems after recovery", "detail": out[-3000:]}
    try:
        verdict = json.loads(out[out.index("{"):])
    except ValueError:
        verdict = {"ok": False, "failures": [out[-1500:] + check.stderr[-500:]]}
    result = {"verify": verdict, "ok": verdict.get("ok") is True}
    if not result["ok"]:
        result["failure"] = "; ".join(verdict.get("failures", []))[:2000]
    return result


ARGS: argparse.Namespace


def main() -> int:
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--busid", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--pull", choices=("kill", "manual"), default="kill")
    parser.add_argument("--letter", default="N")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--min-seconds", type=float, default=3)
    parser.add_argument("--max-seconds", type=float, default=12)
    parser.add_argument("--seed", type=int, default=None)
    ARGS = args = parser.parse_args()

    harness = Path(__file__).resolve().parents[2]
    rng = random.Random(args.seed)
    args.work.mkdir(parents=True, exist_ok=True)
    acks = args.work / "acks"
    acks.mkdir(exist_ok=True)
    results = args.work / "results.jsonl"
    check_bus(args.serial, args.busid)
    failed = False

    for _ in range(args.iterations):
        index = next_index(acks)
        result: dict = {"iteration": index, "pull": args.pull}
        number = to_windows(args.serial)
        result["disk"] = number
        proc = mount(args.program, number, args.letter, args.work / f"ext4win-{index}.log")
        log = acks / f"run-{index}.jsonl"
        env = {**os.environ, "PYTHONPATH": str(harness), "PYTHONDONTWRITEBYTECODE": "1"}
        workload = subprocess.Popen(
            [sys.executable, "-m", "yank.workload", "--target", f"{args.letter}:/run-{index}",
             "--acklog", str(log), "--seed", str(rng.randrange(2**32))],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 90
            while not any(r.get("kind") == "ready" for r in acklog.read(log)):
                if workload.poll() is not None or time.monotonic() > deadline:
                    out = workload.stdout.read().decode(errors="replace")[-1500:] if workload.stdout else ""
                    raise RuntimeError("the workload did not get ready: " + out)
                time.sleep(0.1)
            started = time.monotonic()
            if args.pull == "manual":
                manual_pull(args.serial)
            else:
                time.sleep(rng.uniform(args.min_seconds, args.max_seconds))
                proc.kill()                      # nothing more reaches the drive
            result["pulled_after_seconds"] = round(time.monotonic() - started, 2)
        finally:
            workload.kill()
            workload.wait(timeout=60)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=60)

        records = acklog.read(log)
        result["acknowledged"] = sum(1 for r in records if r.get("op") == "ack")
        to_wsl(args.serial)
        result.update(judge(args.serial, harness, acks, index))

        with open(results, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")
        checked = sum(result.get("verify", {}).get("checked", {}).values())
        print(f"iteration {index:3d}: {'PASS' if result.get('ok') else 'FAIL'}  {args.pull} pull after "
              f"{result.get('pulled_after_seconds')}s, {result['acknowledged']} acknowledged, "
              f"checked {checked} across all runs", flush=True)
        if not result.get("ok"):
            print("  " + str(result.get("failure")), flush=True)
            failed = True
            break
    print(f"results: {results}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
