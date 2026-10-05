# Code signing policy

SignPath requires this page to exist on the project's home page before it will
sign anything, and it is worth having regardless: it tells anyone who checks a
signature what that signature actually promises.

## Who signs the binaries, and with whose certificate

Free code signing is provided by [SignPath.io](https://signpath.io), with a
certificate issued by the [SignPath Foundation](https://signpath.org).

**The certificate names SignPath Foundation as the publisher.** Windows will
show *SignPath Foundation*, not this project and not its author. That is how
the Foundation's programme works for every project in it. A signature therefore
says two things, and only these two:

1. The binary was built by this project's automated build, from the source in
   `https://github.com/Eyekeepers/ext4-windows`, at the commit the release records.
2. Nothing has altered the binary since.

It is not an endorsement by SignPath, by Microsoft, or by anyone else.

## How a release is built and signed

Signing is not something a person does on a laptop. Every signed binary is
produced by the project's GitHub Actions build and submitted to SignPath from
inside that build, so the signature is tied to the source rather than to
whoever happened to run a compiler.

1. A release tag is pushed.
2. GitHub Actions builds the program from that exact commit.
3. The build submits the artifact to SignPath, which verifies it came from this
   repository's trusted build system (*origin verification*).
4. A human **approver** confirms the signing request.
5. The signed binary is published on the repository's Releases page with its
   SHA-256.

Step 4 is deliberate and permanent. It is the step that makes the signature
mean a person decided to publish this build.

## Roles

| Role | Who |
|---|---|
| Author | contributors who open pull requests |
| Reviewer | the maintainer, who reviews and merges |
| Approver | the maintainer, who approves each signing request |

Multi-factor authentication is required for the repository and for SignPath, for
everyone holding a role.

## Privacy

**This program collects nothing and sends nothing.** It mounts a filesystem on
the computer it is run on. It makes no network connections, has no telemetry, no
analytics and no update check, and it writes nothing outside the drive it was
asked to mount and a status file it is explicitly told to write.

There is therefore no data collection to disclose and no privacy policy to
display during installation.

## Uninstalling

The program is a single executable and its runtime library; removing those files
removes it. It installs no service and writes nothing to the registry.

The Dokany driver it depends on is a separate, Microsoft-signed product with its
own installer and uninstaller, and it is not bundled here. Removing it is done
through Windows' own list of installed programs.

**Nothing on the drive is touched by uninstalling**, which is the point: the
drive holds a person's files, and a program that reads them should be removable
without consequence.

## Verifying a release yourself

Every release records the commit it was built from and the SHA-256 of each
artifact. The build is reproducible from that commit, and the GitHub Actions log
for the release is public. If a binary's checksum does not match what the
release records, do not run it.
