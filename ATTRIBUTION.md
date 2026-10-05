# What this program is made of, and under what terms

This program is **GPL-2.0**, and that is not a preference. It links the Linux
kernel's own ext4 implementation, so the combined work is a derivative of the
kernel and carries the kernel's licence. The full text is in [LICENSE](LICENSE).

The kernel's GPL-2.0 carries the Linux-syscall-note, which exempts ordinary
userspace programs that merely *call* Linux system calls. That exemption does
not apply here: this program is linked against kernel code rather than calling
into a kernel across a syscall boundary.

## Components

| Component | What it does here | Licence | How it is used |
|---|---|---|---|
| **LKL** (Linux Kernel Library) | the real Linux ext4 code, including its journal and replay, running inside this program | GPL-2.0 WITH Linux-syscall-note | linked statically |
| **Dokany** | the Windows filesystem driver and its user-mode library, so Windows can see the drive | LGPL-3.0 and MIT (dual) — [LGPL-3.0](licenses/dokany-LGPL-3.0.txt), [MIT](licenses/dokany-MIT.txt) | `dokan2.dll` linked dynamically; the driver is installed separately and not redistributed here |
| **Cygwin runtime** (`msys-2.0.dll`) | the C runtime the MSYS2 build depends on | LGPL-3.0 | distributed alongside the executable, unmodified |

A copy of the LGPL-2.1 text is kept in [licenses/](licenses/) because parts of
the kernel tree are offered under it.

## The Dokany driver is signed by Microsoft, and we rely on that

`dokan2.sys` is signed by the *Microsoft Windows Hardware Compatibility
Publisher*. That is what makes this approach possible at all: a kernel driver on
Windows 11 must be signed through Microsoft, which otherwise needs an extended
validation certificate costing hundreds of dollars a year plus a hardware token.
We never sign a kernel driver and never ask anyone to disable driver signing.

Verified before the driver was installed; the record is in
[docs/dokany-verification.md](docs/dokany-verification.md).

## What is not in here

No part of the product this program was written for is included — no
configuration, no agent, no data, no proprietary code. That separation is
deliberate: a GPL-2.0 program that another program merely *uses*, through a
mounted filesystem rather than by linking, keeps its own licence to itself.

## Source

Anyone receiving a binary is entitled to the source it was built from. The
repository is public and every release records the commit it was built at, so
the source offer is satisfied by the repository itself.
