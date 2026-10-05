"""0c step 3 on Windows: the same workload, through jhext4, pulled by killing it.

    python -m yank.drivers.windows_kill --jhext4 jhext4.exe --image drive.img
           --work DIR [--fresh 6G] [--part 1] [--iterations 10] [--letter N]
           [--min-seconds 3 --max-seconds 12]

Run from Windows, in this harness/ folder. Each iteration mounts the image
read/write through jhext4, runs the workload into a new folder on the drive,
and at a random moment kills jhext4 outright. The disk handle is write-through
and unbuffered, so nothing of ours lingers in Windows' cache after the kill:
from the disk's point of view this is the drive vanishing mid-write.

Then Linux (WSL) is the judge, exactly as in the Linux reference: replay the
journal, `e2fsck -fn` must be clean, and the verifier checks every write the
log says was acknowledged -- in this run and every earlier one on the drive.

PostgreSQL is not in this run: its Windows build is Project 4. SQLite,
atomic replaces, appends, copies and credential files are.

The image must be one this harness made (`--fresh`), never a real drive.
`--fresh` lays it out the way prepare_drive.sh lays out a drive -- GPT, one
partition -- so the program is asked to do on an image what it does on the
stick. The first step 3 run used a whole-disk image instead and so passed
`--part 0`; its numbers are recorded that way in docs/0c-step3-read-write.md.
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

#
# Judge the image in Linux. The work happens in /var/tmp, never /tmp: /tmp in
# WSL is a tmpfs of a few GB -- a RAM disk -- so a copy of a drive-sized image
# is silently truncated there and the loop device comes back zero-sized, which
# reads as "the filesystem is broken" when nothing was ever written.
#
# It is a copy because the image lives on a Windows path, which cannot be
# loop-mounted, and because the verifier mounts read/write so SQLite can finish
# its own recovery -- as it would on a re-plugged drive.
#
WSL_CHECK = r"""
set -e
img="$1"; part="$2"; shift 2
work=/var/tmp/yankw
mkdir -p "$work/mnt"
cp "$img" "$work/drive.img"
want=$(stat -c %s "$img"); got=$(stat -c %s "$work/drive.img")
[ "$want" = "$got" ] || { echo "COPY_SHORT $got of $want bytes into $work; df:"; df -h "$work"; exit 0; }
loop=$(losetup -fP --show "$work/drive.img")
cleanup() { umount "$work/mnt" 2>/dev/null || true; losetup -d "$loop"; }
trap cleanup EXIT
dev="$loop"
if [ "$part" != 0 ]; then
    dev="${loop}p${part}"
    for _ in $(seq 1 40); do [ -b "$dev" ] && break; sleep 0.25; done
    [ -b "$dev" ] || { echo "NO_PARTITION $dev did not appear; partition table:"; partx -s "$loop" 2>&1 | head; exit 0; }
fi
e2fsck -fy "$dev" >"$work/fsck-replay.txt" 2>&1 || true   # the journal replay, as a re-plug does it
if e2fsck -fn "$dev" >"$work/fsck-check.txt" 2>&1; then echo FSCK_CLEAN; else echo FSCK_DIRTY; tail -20 "$work/fsck-check.txt"; exit 0; fi
mount "$dev" "$work/mnt"
cd "$HARNESS"
python3 -m yank.verify "$@" || true
"""


def wsl_path(path: Path) -> str:
    p = str(path.resolve())
    return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")


FRESH = r"""
set -e
img="$1"; size="$2"
rm -f "$img"
truncate -s "$size" "$img"
parted -s -- "$img" mklabel gpt mkpart primary ext4 1MiB 100%
loop=$(losetup -fP --show "$img")
trap 'losetup -d "$loop"' EXIT
for _ in $(seq 1 40); do [ -b "${loop}p1" ] && break; sleep 0.25; done
mkfs.ext4 -q -F -L EXT4TEST -- "${loop}p1"
sync
"""


def fresh(image: Path, size: str) -> None:
    """prepare_drive.sh:121 and :134, on an image, from Linux: one partition in
    a GPT, so the program is asked on an image exactly what the drive asks."""
    done = subprocess.run(["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e", "bash", "-c", FRESH,
                           "fresh", wsl_path(image), size], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"the image could not be made: {done.stdout}{done.stderr}".strip())


def mount(jhext4: Path, image: Path, part: int, letter: str, log: Path) -> subprocess.Popen:
    stream = open(log, "ab")
    proc = subprocess.Popen([str(jhext4), "--disk", str(image), "--part", str(part),
                             "--mount", letter, "--read-write"],
                            stdout=stream, stderr=stream, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    stream.close()
    root = Path(f"{letter}:/")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"jhext4 exited while mounting; see {log}")
        if root.exists():
            return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("the drive did not appear")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--jhext4", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--letter", default="N")
    parser.add_argument("--fresh", metavar="SIZE", help="make the image first, laid out as a drive is")
    parser.add_argument("--part", type=int, default=1, help="0 for a whole-disk image")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--min-seconds", type=float, default=3)
    parser.add_argument("--max-seconds", type=float, default=12)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    harness = Path(__file__).resolve().parents[2]
    rng = random.Random(args.seed)
    if args.fresh:
        fresh(args.image, args.fresh)
    args.work.mkdir(parents=True, exist_ok=True)
    acks = args.work / "acks"
    acks.mkdir(exist_ok=True)
    results = args.work / "results.jsonl"
    failed = False

    for index in range(args.iterations):
        result: dict = {"iteration": index}
        proc = mount(args.jhext4, args.image, args.part, args.letter, args.work / f"jhext4-{index}.log")
        log = acks / f"run-{index}.jsonl"
        env = {**os.environ, "PYTHONPATH": str(harness), "PYTHONDONTWRITEBYTECODE": "1"}
        workload = subprocess.Popen(
            [sys.executable, "-m", "yank.workload", "--target", f"{args.letter}:/run-{index}",
             "--acklog", str(log), "--seed", str(rng.randrange(2**32))],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 60
            while not any(r.get("kind") == "ready" for r in acklog.read(log)):
                if workload.poll() is not None or time.monotonic() > deadline:
                    out = workload.stdout.read().decode(errors="replace")[-1500:] if workload.stdout else ""
                    raise RuntimeError("the workload did not get ready: " + out)
                time.sleep(0.1)
            wait = rng.uniform(args.min_seconds, args.max_seconds)
            time.sleep(wait)
            proc.kill()                      # the drive vanishes
            result["pulled_after_seconds"] = round(wait, 2)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            workload.kill()
            workload.wait(timeout=30)

        records = acklog.read(log)
        result["acknowledged"] = sum(1 for r in records if r.get("op") == "ack")

        runs = []
        for done in range(index + 1):
            runs += ["--run", f"/var/tmp/yankw/mnt/run-{done}", wsl_path(acks / f"run-{done}.jsonl")]
        check = subprocess.run(
            ["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e", "env", f"HARNESS={wsl_path(harness)}",
             "PYTHONDONTWRITEBYTECODE=1", "bash", "-c", WSL_CHECK, "check", wsl_path(args.image),
             str(args.part), *runs],
            capture_output=True, text=True)
        out = check.stdout
        # A harness fault must never read as a filesystem fault: say which it was.
        if out.startswith(("COPY_SHORT", "NO_PARTITION")) or not out.strip():
            result.update(ok=False, failure="the check could not run: " + (out.strip() or "no output"),
                          detail=(out + check.stderr)[-2000:], harness_fault=True)
        elif "FSCK_CLEAN" not in out:
            result.update(ok=False, failure="e2fsck -fn found problems after replay",
                          detail=(out + check.stderr)[-2000:])
        else:
            try:
                verdict = json.loads(out[out.index("{"):])
            except ValueError:
                verdict = {"ok": False, "failures": [out[-1500:] + check.stderr[-500:]]}
            result["verify"] = verdict
            result["ok"] = verdict.get("ok") is True
            if not result["ok"]:
                result["failure"] = "; ".join(verdict.get("failures", []))[:2000]

        with open(results, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")
        checked = sum(result.get("verify", {}).get("checked", {}).values())
        print(f"iteration {index:3d}: {'PASS' if result.get('ok') else 'FAIL'}  pulled after "
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
