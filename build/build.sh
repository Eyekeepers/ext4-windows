#!/bin/bash
#
# Build ext4win.exe under MSYS2 (MSYSTEM=MSYS), against an LKL tree that
# patches/apply_virtio_blk_flush.py has already fixed.
#
#   bash build/build.sh [--lkl DIR] [--out DIR]
#
# The build happens inside LKL's own tests/ directory, through its Targets and
# tests/Build files. Linking by hand against lkl.o fails with "relocation
# truncated to fit": LKL is one large object that expects the kernel's own link
# arrangement, and the test harness is the arrangement that works.
#
# No `make -j`: LKL's headers_install.py reads the make flags and mis-parses
# -j, so a parallel build fails in a way that reads like a source error.
#
set -euo pipefail

lkl=lkl
out=out
while [ $# -gt 0 ]; do
    case "$1" in
        --lkl) lkl=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

root=$(cd "$(dirname "$0")/.." && pwd)
lkl=$(cd "$lkl" && pwd)
vendor="$root/vendor"
target="$lkl/tools/lkl"

[ -d "$target/tests" ] || { echo "not an LKL tree: $lkl" >&2; exit 1; }
# Dokany's headers include each other by plain name, so they sit flat in
# vendor/include and one -I covers all three. build/pins.json says where they
# come from in Dokany's own tree.
for f in include/dokan.h include/fileinfo.h include/public.h lib/dokan2.dll; do
    [ -f "$vendor/$f" ] || { echo "missing vendor/$f -- see build/pins.json" >&2; exit 1; }
done

echo "== source"
cp -f "$root/src/ext4win.c" "$target/tests/ext4win.c"

echo "== registering the target"
# Targets names the program and what it links; tests/Build names the object and
# its include path. Both are needed, and both are idempotent here so a second
# build in the same tree does not append duplicates.
grep -q 'progs-y += tests/ext4win' "$target/Targets" || {
    printf 'progs-y += tests/ext4win\n' >> "$target/Targets"
    printf 'LDLIBS_tests/ext4win-y += %s/lib/dokan2.dll\n' "$vendor" >> "$target/Targets"
}
grep -q 'ext4win-y += ext4win.o' "$target/tests/Build" || {
    printf 'ext4win-y += ext4win.o\n' >> "$target/tests/Build"
    printf 'CFLAGS_ext4win.o += -I$(srctree)/tools/lkl/lib -I%s/include\n' "$vendor" \
        >> "$target/tests/Build"
}

echo "== build"
cd "$lkl"
make -C tools/lkl "$target/tests/ext4win.exe"

echo "== collect"
mkdir -p "$root/$out"
cp -f "$target/tests/ext4win.exe" "$root/$out/ext4win.exe"
# The Cygwin runtime the MSYS2 build needs. Without it the program exits
# 0xC0000135 before printing anything -- measured in
# docs/0c-step5-removal-and-stopping.md. LGPLv3; see ATTRIBUTION.md.
cp -f /usr/bin/msys-2.0.dll "$root/$out/msys-2.0.dll"

ls -la "$root/$out"
