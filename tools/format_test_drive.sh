#!/bin/bash
#
# Format the spare test drive as the product's own tools/prepare_drive.sh formats a
# drive, for the pull tests. Run as root in Linux (WSL, with the drive attached
# through usbipd):
#
#     JH_TEST_SERIAL=<the spare drive's USB serial> bash format_test_drive.sh
#
# It erases the drive. It finds the drive by that serial and nothing else --
# not by device name and not by drive number, both of which move between plugs
# -- and refuses anything that is not on the USB bus or is 40 GB or larger. A
# other drive is therefore out of reach of a typo: naming one would mean
# typing its serial in deliberately.
#
# lsblk's label is not read anywhere here, and must not be: on a USB bridge it
# keeps answering with the old label after a format (see docs/0a-drive-format.md).
set -euo pipefail

: "${JH_TEST_SERIAL:?set JH_TEST_SERIAL to the spare drive's USB serial}"

dev=""
for d in /sys/block/sd*; do
    name=$(basename "$d")
    serial=$(lsblk -dno SERIAL "/dev/$name" 2>/dev/null | tr -d ' ')
    if [ "$serial" = "$JH_TEST_SERIAL" ]; then dev="/dev/$name"; fi
done
[ -n "$dev" ] || { echo "no drive with serial $JH_TEST_SERIAL is attached"; exit 1; }
[ "$(lsblk -dno TRAN "$dev")" = "usb" ] || { echo "refusing: $dev is not usb"; exit 1; }
size=$(lsblk -bdno SIZE "$dev")
[ "$size" -lt 40000000000 ] || { echo "refusing: $dev is $size bytes"; exit 1; }
echo "formatting $dev ($size bytes, serial $JH_TEST_SERIAL)"
findmnt -rno TARGET -S "${dev}1" && { echo "refusing: mounted"; exit 1; } || true

# prepare_drive.sh:120, :121 and :134.
wipefs -a -- "$dev"
parted -s -- "$dev" mklabel gpt mkpart primary ext4 1MiB 100%
partprobe "$dev" || true
udevadm settle || true
for _ in $(seq 1 20); do [ -b "${dev}1" ] && break; sleep 0.5; done
mkfs.ext4 -F -L EXT4TEST -- "${dev}1"
sync

echo "--- result"
lsblk -o NAME,SIZE,FSTYPE,PARTTYPE,SERIAL "$dev"
blkid -p "${dev}1"
echo "--- superblock"
dumpe2fs -h "${dev}1" 2>/dev/null |
    grep -E '^(Filesystem features|Filesystem flags|Block size|Inode size|Journal size|Filesystem revision|Default mount options)'
mke2fs -V 2>&1 | head -1
