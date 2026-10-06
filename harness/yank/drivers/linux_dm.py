"""The Linux reference: the kernel's own ext4, with the disk pulled by device-mapper.

    sudo python3 -m yank.drivers.linux_dm --work /var/tmp/yank --iterations 20
                 [--pg-payload postgres.tar.gz] [--user NAME]
                 [--size 2G] [--min-seconds 3] [--max-seconds 15]

Root, because it attaches and detaches block devices; the workload and the
verifier run as an ordinary account, as ordinary programs do.

The drive is an image file formatted exactly as docs/0a-drive-format.md
formats a real one. Each iteration mounts it, runs the workload into a new
folder, and at a random moment pulls the disk:

    dmsetup suspend --noflush --nolockfs, then the `error` target

so whatever the kernel had not yet written never reaches the disk and every
later write fails, which is what a vanished disk looks like from above. Then it
plugs the disk back in: mount (the journal replays), unmount, `e2fsck -fn`, and
the verifier over *every* folder written so far, because a pull must not damage
what earlier iterations saved either.

What this simulates is a disk with no write cache of its own: a write the disk
completed is kept, because the image lives on the host. A real USB drive's own
cache is the spare test drive's to answer, in 0c step 7. This run is the bar
the Windows program must meet, and the proof that the harness catches loss.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import acklog

HARNESS = Path(__file__).resolve().parents[2]


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=check, capture_output=True, text=True)


class Disk:
    def __init__(self, image: Path, mountpoint: Path) -> None:
        self.image = image
        self.mountpoint = mountpoint
        self.loop = ""

    def create(self, size: str) -> None:
        """docs/0a-drive-format.md's two commands, with an image in place of the device."""
        self.image.unlink(missing_ok=True)
        run("truncate", "-s", size, str(self.image))
        run("parted", "-s", "--", str(self.image), "mklabel", "gpt", "mkpart", "primary", "ext4", "1MiB", "100%")
        partition = self.attach()
        run("mkfs.ext4", "-q", "-F", "-L", "EXT4TEST", "--", partition)
        self.detach()

    def attach(self) -> str:
        self.loop = run("losetup", "-fP", "--show", str(self.image)).stdout.strip()
        partition = self.loop + "p1"
        for _ in range(50):
            if Path(partition).exists():
                return partition
            time.sleep(0.1)
        raise RuntimeError(f"{partition} did not appear")

    def detach(self) -> None:
        if self.loop:
            run("losetup", "-d", self.loop, check=False)
            self.loop = ""

    def mount(self, device: str) -> None:
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        run("mount", device, str(self.mountpoint))

    def umount(self, lazy: bool = False) -> None:
        # The lazy form is the clean-up path and may find nothing mounted.
        run("umount", *(["-l"] if lazy else []), str(self.mountpoint), check=not lazy)


class Pull:
    """A device-mapper device that can be made to vanish."""

    def __init__(self, name: str, partition: str) -> None:
        self.name = name
        self.partition = partition
        self.sectors = run("blockdev", "--getsz", partition).stdout.strip()
        run("dmsetup", "create", name, "--table", f"0 {self.sectors} linear {partition} 0")
        self.device = f"/dev/mapper/{name}"

    def _swap(self, table: str) -> None:
        run("dmsetup", "suspend", "--noflush", "--nolockfs", self.name)
        run("dmsetup", "load", self.name, "--table", f"0 {self.sectors} {table}")
        run("dmsetup", "resume", self.name)

    def pull(self) -> None:
        self._swap("error")

    def lie(self) -> None:
        """The self-test: from now on every write reports success and is thrown
        away, as a disk with a volatile cache does when it loses power. A
        harness that passes after this has not been checking anything."""
        run("modprobe", "dm-flakey", check=False)
        self._swap(f"flakey {self.partition} 0 0 86400 1 drop_writes")

    def remove(self) -> None:
        run("dmsetup", "remove", "--force", "--retry", self.name, check=False)


class Driver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.work: Path = args.work
        self.user: str = args.user or pwd.getpwuid(1000).pw_name
        self.account = pwd.getpwnam(self.user)
        self.rng = random.Random(args.seed)
        self.args = args
        self.disk = Disk(self.work / "drive.img", self.work / "mnt")
        self.acks = self.work / "acks"
        self.results = self.work / "results.jsonl"
        self.pg_bin: Path | None = None
        self.socket = self.work / "sock"

    def own(self, path: Path) -> None:
        os.chown(path, self.account.pw_uid, self.account.pw_gid)

    def as_user(self, *cmd: str) -> list[str]:
        return ["runuser", "-u", self.user, "--", "env", f"PYTHONPATH={HARNESS}", "PYTHONDONTWRITEBYTECODE=1",
                "TMPDIR=" + str(self.work / "tmp"), *cmd]

    def pg_args(self) -> list[str]:
        if not self.pg_bin:
            return []
        return ["--pg-bin", str(self.pg_bin), "--pg-data", str(self.disk.mountpoint / "pgdata"),
                "--pg-socket", str(self.socket), "--pg-port", "55432"]

    def prepare(self) -> None:
        # An interrupted earlier run can leave the drive mounted under work/;
        # removing the folder first would walk into it.
        self.disk.umount(lazy=True)
        if self.work.exists():
            shutil.rmtree(self.work)
        for path in (self.work, self.acks, self.socket, self.work / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.work, 0o755)
        for path in (self.acks, self.socket, self.work / "tmp"):
            self.own(path)
        if self.args.pg_payload:
            target = self.work / "payload"
            target.mkdir()
            run("tar", "-xzf", str(self.args.pg_payload), "-C", str(target), "postgres")
            run("chmod", "-R", "a+rX", str(target))
            self.pg_bin = target / "postgres/bin"
        self.disk.create(self.args.size)
        partition = self.disk.attach()
        self.disk.mount(partition)
        self.own(self.disk.mountpoint)
        if self.pg_bin:
            subprocess.run(self.as_user(str(self.pg_bin / "initdb"), "-D", str(self.disk.mountpoint / "pgdata"),
                                        "-U", "harness", "-A", "trust", "-E", "UTF8", "--no-locale", "--data-checksums"),
                           check=True, stdout=subprocess.DEVNULL)
        self.disk.umount()
        self.disk.detach()

    def iteration(self, index: int) -> dict:
        result: dict = {"iteration": index}
        partition = self.disk.attach()
        pull = Pull(f"yank{os.getpid()}x{index}", partition)
        self.disk.mount(pull.device)
        log = self.acks / f"run-{index}.jsonl"
        command = self.as_user(sys.executable, "-m", "yank.workload", "--target", str(self.disk.mountpoint / f"run-{index}"),
                               "--acklog", str(log), "--seed", str(self.rng.randrange(2**32)),
                               "--pg-id-base", str(index * 10_000_000), *self.pg_args())
        workload = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            while not any(r.get("kind") == "ready" for r in acklog.read(log)):
                if workload.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the workload did not get ready: "
                                       + (workload.stdout.read().decode(errors="replace")[-2000:] if workload.stdout else ""))
                time.sleep(0.1)
            wait = self.rng.uniform(self.args.min_seconds, self.args.max_seconds)
            if self.args.self_test:
                lying = min(2.0, wait / 2)
                time.sleep(wait - lying)
                pull.lie()
                time.sleep(lying)
                result["lied_for_seconds"] = lying
            else:
                time.sleep(wait)
            pull.pull()
            result["pulled_after_seconds"] = round(wait, 2)
        finally:
            try:
                os.killpg(workload.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            workload.wait(timeout=60)
            self.disk.umount(lazy=True)
            pull.remove()
            self.disk.detach()

        records = acklog.read(log)
        result["acknowledged"] = sum(1 for r in records if r.get("op") == "ack")

        # Plugged back in.
        partition = self.disk.attach()
        try:
            try:
                self.disk.mount(partition)
            except subprocess.CalledProcessError as error:
                result.update(ok=False, failure=f"the drive does not mount after the pull: {error.stderr.strip()}")
                return result
            self.disk.umount()
            fsck = run("e2fsck", "-fn", partition, check=False)
            result["fsck_exit"] = fsck.returncode
            if fsck.returncode != 0:
                result.update(ok=False, failure="e2fsck -fn found problems after recovery",
                              fsck=(fsck.stdout + fsck.stderr)[-3000:])
                return result
            self.disk.mount(partition)
            runs = []
            for done in range(index + 1):
                runs += ["--run", str(self.disk.mountpoint / f"run-{done}"), str(self.acks / f"run-{done}.jsonl")]
            verify = subprocess.run(self.as_user(sys.executable, "-m", "yank.verify", *runs, *self.pg_args(),
                                                 "--pg-log", str(self.work / "tmp" / f"verify-pg-{index}.log")),
                                    capture_output=True, text=True)
            try:
                result["verify"] = json.loads(verify.stdout)
            except json.JSONDecodeError:
                result["verify"] = {"ok": False, "failures": [verify.stdout[-1500:] + verify.stderr[-1500:]]}
            result["ok"] = verify.returncode == 0 and result["verify"].get("ok") is True
            if not result["ok"]:
                result["failure"] = "; ".join(result["verify"].get("failures", []))[:2000]
            self.disk.umount()
        finally:
            self.disk.umount(lazy=True)
            self.disk.detach()
        return result

    def main(self) -> int:
        self.prepare()
        if self.args.self_test:
            result = self.iteration(0)
            with open(self.results, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"self_test": True, **result}) + "\n")
            if result.get("ok"):
                print("self-test FAILED: a disk that threw away acknowledged writes was passed as safe")
                return 1
            print("self-test passed: the lying disk was caught —", result.get("failure", "")[:600])
            return 0
        failed = False
        for index in range(self.args.iterations):
            result = self.iteration(index)
            with open(self.results, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(result) + "\n")
            checked = result.get("verify", {}).get("checked", {})
            print(f"iteration {index:3d}: {'PASS' if result.get('ok') else 'FAIL'}  "
                  f"pulled after {result.get('pulled_after_seconds', '?')}s, "
                  f"{result.get('acknowledged', 0)} writes acknowledged this run, fsck {result.get('fsck_exit', '?')}, "
                  f"checked {sum(checked.values())} across all runs", flush=True)
            if not result.get("ok"):
                print(f"  {result.get('failure')}", flush=True)
                shutil.copy2(self.disk.image, self.work / f"failed-{index}.img")
                failed = True
                break
        print(f"results: {self.results}")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--work", type=Path, default=Path("/var/tmp/yank"))
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--size", default="2G")
    parser.add_argument("--min-seconds", type=float, default=3)
    parser.add_argument("--max-seconds", type=float, default=15)
    parser.add_argument("--pg-payload", type=Path)
    parser.add_argument("--user")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--self-test", action="store_true",
                        help="one iteration on a disk that drops acknowledged writes; passes only if that is caught")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("run as root: this attaches and pulls block devices", file=sys.stderr)
        return 2
    return Driver(args).main()


if __name__ == "__main__":
    sys.exit(main())
