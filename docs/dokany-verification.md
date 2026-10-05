# Dokany: checked before installing it

Dokany supplies the Windows filesystem driver this program presents its mount
through. The whole no-purchase plan rests on one claim about it: that its
kernel driver is signed in a way Windows 11 will load, so we never need our own
EV certificate (which would cost $249–500 a year plus a hardware token).

Checked, not assumed, on 2026-10-03.

## Version and integrity

| | |
|---|---|
| Release | v2.3.1.1000, published 2025-09-28 |
| Source | `github.com/dokan-dev/dokany` releases |
| `DokanSetup.exe` | SHA-256 `BF602263A594F595B4FDD8C4E822172B103DE93F07FD6A51A8FF69569BFD1460` |
| `Dokan_x64.msi` | SHA-256 `69FF8CB37BFEC3A75921C85FFD1C6370B50A9EC4ECEF2CF3A009D488DCBF5465` |

Record the hashes here rather than the installers themselves: this repository
is to be published, and it carries source, not vendor binaries. the product pins
the installer by hash the same way `native-sources.json` pins PostgreSQL.

## The installer

`Get-AuthenticodeSignature` reports **Valid** for both files, signed by
`CN=LEOSAC, O=LEOSAC` (Dokany's publisher, a French company registered as a
Private Organization) through `Certum Extended Validation Code Signing 2021
CA`. Extended Validation, so the publisher's identity was verified.

**Note for later:** that certificate runs out on 2026-12-03, about two months
from this check. Already-signed binaries stay valid if they were timestamped,
and renewal is Dokany's business, not ours. But a release signed after it
expires, with no renewal, would be a reason to pause and look again.

## The driver, which is the part that matters

`dokan2.sys`, extracted from the MSI without installing anything
(`msiexec /a`), is signed by:

> **Microsoft Windows Hardware Compatibility Publisher**
> through *Microsoft Windows Third Party Component CA 2014*

That is Microsoft's own signature on the driver, which is exactly what
Windows 10 and 11 require before loading a kernel driver, Secure Boot and
Memory Integrity included. So:

- **the plan's central assumption holds.** We need no certificate, and we are
  not asking an owner to weaken any Windows security setting;
- **what the owner's computer gets** is a Microsoft-signed driver from an
  identified publisher, installed once, which uninstalls cleanly;
- **our own program stays unprivileged** and in user space. It is the thing
  that still needs free signing from SignPath (Project 0e), and Windows will
  show *SignPath Foundation* as its publisher.

## Still to confirm

- That the driver loads and a mount appears, on a machine with Secure Boot and
  Memory Integrity both on. That is 0c step 2.
- Clean uninstall, leaving the drive untouched. 0c step 9.
- Whether Smart App Control, which is stricter again, lets our own unsigned
  development builds run at all. Project 0e.
