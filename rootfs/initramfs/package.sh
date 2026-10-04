#!/bin/sh
# Build the t2-initramfs Debian package from the packed initramfs.
#
# A hand-written DEBIAN/ control directory plus `dpkg-deb --build` (no
# debhelper, no debian/rules): the build image carries dpkg but not dpkg-dev,
# and this package is one file with no maintainer script.  `--root-owner-group`
# normalises the uid/gid and SOURCE_DATE_EPOCH pins every ar/tar member mtime,
# so the same tree builds the same .deb bytes.
#
# Why it exists: the kernel Image does not embed the initramfs - the boot FIT
# carries it as a ramdisk subimage.  The image build assembles that FIT, and an
# on-board kernel upgrade (linux-image-<rel>-t2's postinst, which Depends on
# this package) assembles an identical one from these exact bytes.  The file
# lives under /boot, which on the board is an ordinary rootfs directory: the
# profile's /etc/fstab carries no entries and the boot FAT is mounted on demand
# by t2-utils' t2-boot-commit.sh, so nothing shadows it.
#
# Usage: package.sh [VERSION] [OUT.deb]
#   VERSION  defaults to 0.1.0~<git describe --tags --always --dirty>
#   OUT      defaults to <repo>/build/out/t2-initramfs_<VERSION>_all.deb
#
# rootfs/t2-distro.py builds and installs this package the same way it installs
# t2-utils, so a running board has the initramfs on disk.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)

version=${1:-}
if [ -z "$version" ]; then
    desc=$(git -C "$repo" describe --tags --always --dirty 2>/dev/null || echo unknown)
    version="0.1.0~$(printf '%s' "$desc" | sed 's/[^0-9A-Za-z.+~]//g')"
fi
out=${2:-$repo/build/out/t2-initramfs_${version}_all.deb}

ramdisk=$repo/build/initramfs.gz
[ -f "$ramdisk" ] || {
    echo "error: no $ramdisk - run rootfs/initramfs/build.sh first" >&2
    exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM

mkdir -p "$work/DEBIAN" "$work/boot"
# Installed-Size is a KB estimate.  Derive it from the file's own size: `du` on
# a just-copied file reports allocated blocks, which is not stable enough to
# keep the .deb byte-reproducible (observed: 1 vs 3329 KB for the same bytes).
installed_size=$(( ($(stat -c%s "$ramdisk") + 1023) / 1024 ))
sed -e "s/@VERSION@/$version/g" \
    -e "s/@INSTALLED_SIZE@/$installed_size/g" \
    "$here/control.initramfs.in" > "$work/DEBIAN/control"
install -m 644 "$ramdisk" "$work/boot/initramfs-t2.gz"

: "${SOURCE_DATE_EPOCH:=0}"
export SOURCE_DATE_EPOCH

mkdir -p "$(dirname "$out")"
dpkg-deb --root-owner-group --build "$work" "$out" >/dev/null
echo "$out"
