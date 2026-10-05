# 0a — what a drive's ext4 looks like

Recorded 2026-10-02. No other drive was read: a drive's format is decided by
the computer that formatted it, and older formatting only ever has *fewer*
features than newer, so supporting the newest defaults supports every drive.

## How a drive is made

the product's `tools/prepare_drive.sh` does, on a Linux computer:

```
parted -s -- <device> mklabel gpt mkpart primary ext4 1MiB 100%
mkfs.ext4 -F -L EXT4TEST -- <partition>
```

No feature options are passed, so the features are that computer's
e2fsprogs defaults.

## Measured

The same two commands on a 2 GiB image file, attached as a loop device, in
WSL2 Ubuntu: parted 3.6, e2fsprogs 1.47.2 (2025-01-01), kernel
6.18.33.2-microsoft-standard-WSL2. The image was deleted afterwards.

| Property | Value |
|---|---|
| Partition table | GPT, one partition from 1 MiB to the end |
| Partition type GUID | `0fc63daf-8483-4772-8e79-3d69d8477de4` (Linux filesystem data) |
| Partition name | `primary` |
| Filesystem label | `EXT4TEST` |
| Features | `has_journal ext_attr resize_inode dir_index orphan_file filetype extent 64bit flex_bg metadata_csum_seed sparse_super large_file huge_file dir_nlink extra_isize metadata_csum` |
| Flags | `signed_directory_hash` |
| Default mount options | `user_xattr acl` |
| Errors behaviour | Continue |
| Block size | 4096 |
| Inode size | 256 |

## What follows

- **The program needs a kernel that supports `orphan_file`** (Linux 5.15 or
  newer) to mount these drives read-write. `metadata_csum_seed` and
  `metadata_csum` need nothing newer than that. Older drives lack
  `orphan_file` and/or `metadata_csum_seed`; the same kernel handles them.
- **Windows will not offer to format the drive.** Windows assigns drive
  letters only to partitions typed as Windows data. This partition is typed
  Linux filesystem data, so it gets no letter and no "format this disk"
  prompt. To be observed directly in 0c step 2, when a VHDX made this way is
  attached to Windows.
- **The ext4 UUID** (needed for the product's licence identity on Windows) is in
  the superblock, which the program reads anyway.

## Still to confirm

The spare test USB drive, formatted by `prepare_drive.sh` on a Linux computer,
in 0c step 7: its real feature list goes here beside this one.
