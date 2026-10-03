#!/usr/bin/env bash
#
# Build every artefact, in the order docs/building.md documents.
#
#   1  rootfs/fetch.sh            vendor WiFi/BT firmware    (once, not automated)
#   2  rootfs/initramfs/build.sh  -> build/initramfs
#   3  kernel/fetch.sh            -> build/kernel
#   4  kernel/build.sh            -> build/out/Image, rk3568-t2.dtb, modules/
#   5  u-boot/fetch.sh            -> build/uboot, build/rkbin
#   6  u-boot/build.sh            -> build/out/u-boot.itb, idbloader.img, ...
#   7  rootfs/build.sh            -> build/out/rootfs.ext4.zst
#   8  images/build-installer.sh  -> build/out/installer.img
#
# Re-runnable: step 3 is skipped when build/kernel already exists (kernel/fetch.sh
# refuses to clobber a tree), step 5 skips a tree that is there, and step 2 is
# skipped when build/initramfs is already laid out.  JOBS overrides the build
# parallelism (default: nproc).
#
# Step 1 cannot be automated: the vendor firmware is not distributable, so it
# comes from a T2 that still runs the vendor firmware, or from a vendor update
# package.  This script reports it when it is missing instead of guessing.
#
# Usage: build-all.sh [-h]
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

case ${1:-} in
-h | --help)
    sed -n '3,17p' "$0" | cut -c3-
    exit 0
    ;;
"") ;;
*)
    echo "build-all: unknown argument '$1'" >&2
    exit 2
    ;;
esac

export JOBS=${JOBS:-$(nproc)}

fw=rootfs/firmware/brcm/fw_bcm43752a2_pcie_ag.bin
if [ ! -f "$fw" ]; then
    echo "build-all: the vendor WiFi/BT firmware is missing: $fw" >&2
    echo "           fetch it once from a T2, or from a vendor update package:" >&2
    echo "             rootfs/fetch.sh --from-host root@<t2>" >&2
    echo "             rootfs/fetch.sh --ota <url>" >&2
    exit 1
fi

echo "== 1/8 vendor firmware =="
echo "  rootfs/firmware/: $(find rootfs/firmware -type f | wc -l) file(s)"

echo "== 2/8 initramfs =="
if [ -f build/initramfs/init ]; then
    echo "  already laid out: build/initramfs (rootfs/initramfs/build.sh redoes it)"
else
    rootfs/initramfs/build.sh
fi

echo "== 3/8 kernel tree =="
if [ -d build/kernel ]; then
    echo "  already present: build/kernel"
else
    kernel/fetch.sh
fi

echo "== 4/8 kernel =="
kernel/build.sh

echo "== 5/8 u-boot sources =="
u-boot/fetch.sh

echo "== 6/8 u-boot =="
u-boot/build.sh

echo "== 7/8 rootfs =="
rootfs/build.sh

echo "== 8/8 installer image =="
images/build-installer.sh

echo
echo "build-all: done."
