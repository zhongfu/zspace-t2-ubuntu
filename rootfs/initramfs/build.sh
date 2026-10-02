#!/usr/bin/env bash
#
# Build the ZSpace T2 bring-up initramfs into <repo>/build/initramfs.
#
# The kernel embeds this tree (CONFIG_INITRAMFS_SOURCE), so kernel/build.sh
# needs it before it runs: firmware fetch -> initramfs -> kernel -> u-boot ->
# rootfs -> installer image.
#
# Steps:
#   1. download the pinned BusyBox 1.36.1 source and verify its sha256
#   2. build it static aarch64 with rootfs/initramfs/busybox.config
#   3. `make install` lays out busybox + every applet symlink
#   4. compile src/t2-keywait.c static aarch64
#   5. copy init, installer.sh and the firmware tree into place
#
# The 1.2 MB busybox binary and the applet symlinks are build outputs, not
# repository content.  The Broadcom WiFi/BT blobs are copied from
# rootfs/firmware/ (populated by rootfs/fetch.sh); the RTL NIC firmware and
# the regulatory database are committed under rootfs/initramfs/firmware/.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)

out=$repo/build/initramfs
jobs=${JOBS:-$(nproc)}
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}

BB_VERSION=1.36.1
BB_URL=${BUSYBOX_URL:-"https://busybox.net/downloads/busybox-$BB_VERSION.tar.bz2"}
BB_SHA256=b8cc24c9574d809e7279c3be349795c5d5ceb6fdf19ca709f80cde50e47de314

downloads=$repo/build/downloads
srcdir=$repo/build/busybox-$BB_VERSION

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] [--out DIR] [-j JOBS]

Build the bring-up initramfs from rootfs/initramfs into build/initramfs.

Options:
  --out DIR      write the tree to DIR (default: $out)
  -j, --jobs N   make jobs (default: $jobs)
  -h, --help     show this help

Environment:
  CROSS_COMPILE  toolchain prefix (default: $CROSS_COMPILE)
  BUSYBOX_URL    source tarball URL override (default: $BB_URL)
EOF
    exit 0
}

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage ;;
        --out)     out=$2; shift 2 ;;
        -j|--jobs) jobs=$2; shift 2 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -f "$here/busybox.config" ] || die "busybox.config is missing next to $0"
[ -f "$here/src/t2-keywait.c" ] || die "src/t2-keywait.c is missing"
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
    || die "need curl or wget to download BusyBox"

command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1 || die \
    "${CROSS_COMPILE}gcc is not on PATH; install an aarch64 cross toolchain \
or set CROSS_COMPILE"

# The Broadcom WiFi/BT firmware is not redistributable, so it is fetched, not
# committed.  The initramfs carries it for the early brcmfmac probe.
fw_blobs=$repo/rootfs/firmware/brcm
if [ ! -d "$fw_blobs" ]; then
    die "$fw_blobs is missing; run rootfs/fetch.sh first (it copies the
vendor Broadcom firmware from a T2 or a vendor update package)"
fi

# --------------------------------------------------------------------- source
mkdir -p "$downloads" "$out"
tarball=$downloads/busybox-$BB_VERSION.tar.bz2
if [ ! -f "$tarball" ]; then
    echo "== downloading BusyBox $BB_VERSION =="
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$tarball.part" "$BB_URL"
    else
        wget -q -O "$tarball.part" "$BB_URL"
    fi
    mv "$tarball.part" "$tarball"
fi

got=$(sha256sum "$tarball" | awk '{print $1}')
[ "$got" = "$BB_SHA256" ] || die "BusyBox tarball sha256 $got != $BB_SHA256"

if [ ! -d "$srcdir" ]; then
    echo "== extracting to $srcdir =="
    tar -xjf "$tarball" -C "$repo/build" --no-same-owner
fi

# ---------------------------------------------------------------------- build
# The cross gcc defaults its sysroot to / on some distributions, so route the
# arm64 libc headers and libraries explicitly - the same flags the original
# recipe used (notes/mainline-7.3-build.md).
extra_cflags=""
extra_ldflags=""
if [ -d /usr/aarch64-linux-gnu/include ]; then
    extra_cflags="-I/usr/aarch64-linux-gnu/include -B/usr/aarch64-linux-gnu/lib"
fi
if [ -d /usr/aarch64-linux-gnu/lib ]; then
    extra_ldflags="-L/usr/aarch64-linux-gnu/lib -B/usr/aarch64-linux-gnu/lib"
fi

cp -f "$here/busybox.config" "$srcdir/.config"

# CFLAGS/LDFLAGS must be *environment* variables: BusyBox's Makefile does
# `CFLAGS := $(CFLAGS)` and then appends its own flags, so a command-line
# assignment would override the file's appends and lose them.
echo "== building BusyBox $BB_VERSION (static aarch64, -j$jobs) =="
CFLAGS="$extra_cflags" LDFLAGS="$extra_ldflags" \
    make -C "$srcdir" -j"$jobs" CROSS_COMPILE="$CROSS_COMPILE"

echo "== installing busybox + applet symlinks into $out =="
CFLAGS="$extra_cflags" LDFLAGS="$extra_ldflags" \
    make -C "$srcdir" CROSS_COMPILE="$CROSS_COMPILE" \
    CONFIG_PREFIX="$out" install >/dev/null

echo "== building t2-keywait =="
mkdir -p "$out/bin"
"${CROSS_COMPILE}gcc" \
    -isystem /usr/aarch64-linux-gnu/include \
    -B/usr/aarch64-linux-gnu/lib -L/usr/aarch64-linux-gnu/lib \
    -static -O2 -s -o "$out/bin/t2-keywait" "$here/src/t2-keywait.c"

# -------------------------------------------------------------------- layout
for d in dev etc proc root sys tmp var/run; do
    mkdir -p "$out/$d"
done
install -m 755 "$here/init" "$out/init"
install -m 755 "$here/installer.sh" "$out/installer.sh"

echo "== firmware tree =="
mkdir -p "$out/lib/firmware/rtl_nic" "$out/lib/firmware/brcm"
cp -f "$here/firmware/rtl_nic/"*.fw "$out/lib/firmware/rtl_nic/"
cp -f "$here/firmware/regulatory.db" "$here/firmware/regulatory.db.p7s" \
      "$out/lib/firmware/"
shopt -s nullglob
blobs=("$fw_blobs"/*.bin "$fw_blobs"/*.txt "$fw_blobs"/*.clm_blob
       "$fw_blobs"/*.hcd)
shopt -u nullglob
[ ${#blobs[@]} -gt 0 ] || die "$fw_blobs holds no vendor firmware; run rootfs/fetch.sh"
cp -f "${blobs[@]}" "$out/lib/firmware/brcm/"
chmod 644 "$out/lib/firmware/rtl_nic/"* "$out/lib/firmware/"*.db* \
       "$out/lib/firmware/brcm/"* 2>/dev/null || true

echo
echo "initramfs tree: $out"
ls -l "$out/bin/busybox" "$out/bin/t2-keywait"
echo "  applet symlinks: $(find "$out/bin" "$out/sbin" -type l | wc -l)"
echo "  firmware:        $(find "$out/lib/firmware" -type f | wc -l) file(s)"
echo "done."
