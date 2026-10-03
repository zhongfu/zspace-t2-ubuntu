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
# refuses to clobber a tree) and step 5 skips a tree that is there.
#
# Steps 2, 4, 6, 7 and 8 also record a stamp of their inputs under build/.stamps/:
# a rerun whose inputs hash the same prints "[skip] ..." and rebuilds nothing.
# Step 2's inputs are rootfs/initramfs/ (its output is embedded in the kernel
# Image, so it must not be left stale); step 7's are the profile tree, the
# userspace package source (rootfs/packages/), the firmware trees, the kernel
# artefacts (build/out/Image, rk3568-t2.dtb, modules/) and the scripts it
# invokes; step 8's are the five artefacts, the boot-tree tools and the config
# template.
#
# Steps can be selected: --only, --skip, --from, --to, --list.  For example,
# `--only 6,8` rebuilds U-Boot and the installer image only.
#
# Step 1 cannot be automated: the vendor firmware is not distributable, so it
# comes from a T2 that still runs the vendor firmware, or from a vendor update
# package.  This script reports it when it is missing instead of guessing.
#
# Usage: build-all.sh [--only N[,N] | --skip N | --from N | --to N] [-h|--help|--list]
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

build_dir=$here/build
stamp_dir=$build_dir/.stamps

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Build the ZSpace T2 artefacts in order (see docs/building.md).

Options:
  --only N[,N]   run only these steps (1..8); leave the rest alone
  --skip N[,N]   do not run these steps
  --from N       start at step N (inclusive)
  --to N         stop after step N (inclusive)
  --list         list the steps and exit
  -h, --help     show this help

Steps:
  1  vendor firmware      rootfs/fetch.sh (external; the tree is checked)
  2  initramfs            rootfs/initramfs/build.sh    (stamped)
  3  kernel tree          kernel/fetch.sh
  4  kernel               kernel/build.sh              (stamped)
  5  u-boot sources       u-boot/fetch.sh
  6  u-boot               u-boot/build.sh              (stamped)
  7  rootfs               rootfs/build.sh              (stamped)
  8  installer image      images/build-installer.sh    (stamped)

Steps 3 and 5 are skipped when their output tree is already there; steps 2, 4,
6, 7 and 8 are skipped when the sha256 of their inputs matches the stamp under
build/.stamps/.

Environment:
  JOBS   build parallelism (default: \$(nproc))
EOF
}

list_steps() {
    cat <<EOF
1  vendor firmware      rootfs/fetch.sh
2  initramfs            rootfs/initramfs/build.sh
3  kernel tree          kernel/fetch.sh
4  kernel               kernel/build.sh
5  u-boot sources       u-boot/fetch.sh
6  u-boot               u-boot/build.sh
7  rootfs               rootfs/build.sh
8  installer image      images/build-installer.sh
EOF
}

only=
skip=
from=
to=

while [ $# -gt 0 ]; do
    case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
    --list)
        list_steps
        exit 0
        ;;
    --only | --skip | --from | --to)
        [ $# -ge 2 ] || { echo "build-all: $1 needs an argument" >&2; exit 2; }
        case "$1" in
        --only) only=$2 ;;
        --skip) skip=$2 ;;
        --from) from=$2 ;;
        --to) to=$2 ;;
        esac
        shift 2
        ;;
    *)
        echo "build-all: unknown argument '$1' (try --help)" >&2
        exit 2
        ;;
    esac
done

# Every value is one or more comma-separated step numbers.
check_list() { # option value
    local n
    for n in ${2//,/ }; do
        case "$n" in
        [1-8]) ;;
        *) echo "build-all: $1 '$n' is not a step number (1..8)" >&2; exit 2 ;;
        esac
    done
}

check_list --only "$only"
check_list --skip "$skip"
[ -z "$from" ] || check_list --from "$from"
[ -z "$to" ] || check_list --to "$to"

# wanted N - is step N selected?
wanted() {
    local n=$1
    if [ -n "$only" ]; then
        case ",$only," in
        *",$n,"*) ;;
        *) return 1 ;;
        esac
    else
        if [ -n "$from" ] && [ "$n" -lt "$from" ]; then return 1; fi
        if [ -n "$to" ] && [ "$n" -gt "$to" ]; then return 1; fi
    fi
    if [ -n "$skip" ]; then
        case ",$skip," in
        *",$n,"*) return 1 ;;
        esac
    fi
    return 0
}

# hash_inputs <path>... - emit a (path, sha256) listing.  Directories are walked
# with find; symlinks contribute their target.  Content only, so a bare `touch`
# does not change the hash.
hash_inputs() {
    local p
    for p in "$@"; do
        [ -e "$p" ] || continue
        if [ -f "$p" ]; then
            sha256sum "$p"
        elif [ -d "$p" ]; then
            find "$p" -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
            find "$p" -type l -printf 'symlink %p -> %l\n'
        fi
    done
}

# step_hash <marker> <inputs...> - sha256 of the listing plus the marker (for a
# fact no file carries, e.g. a git HEAD).
step_hash() {
    local marker=$1
    shift
    { printf '%s\n' "$marker"; hash_inputs "$@"; } \
        | LC_ALL=C sort | sha256sum | cut -d' ' -f1
}

outputs_exist() {
    local p
    for p in "$@"; do
        [ -e "$p" ] || return 1
    done
    return 0
}

# stamp_skip <N> <label> <stamp name> <hash> <outputs...>
stamp_skip() {
    local n=$1 label=$2 name=$3 hash=$4
    shift 4
    local stamp=$stamp_dir/$name
    if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$hash" ] \
            && outputs_exist "$@"; then
        echo "[skip] $n/8 $label: unchanged ($stamp)"
        return 0
    fi
    return 1
}

stamp_write() { # <stamp name> <hash>
    mkdir -p "$stamp_dir"
    printf '%s\n' "$2" > "$stamp_dir/$1"
}

selected=
for n in 1 2 3 4 5 6 7 8; do
    if wanted "$n"; then selected="$selected $n"; fi
done
echo "build-all: steps$selected (of 1..8)"

export JOBS=${JOBS:-$(nproc)}

# The firmware check is a precondition of step 1 (it reports the tree) and of
# step 7 (rootfs/build.sh stops without it); a run that selects neither does not
# need the firmware.
if wanted 1 || wanted 7; then
    fw=rootfs/firmware/brcm/fw_bcm43752a2_pcie_ag.bin
    if [ ! -f "$fw" ]; then
        echo "build-all: the vendor WiFi/BT firmware is missing: $fw" >&2
        echo "           fetch it once from a T2, or from a vendor update package:" >&2
        echo "             rootfs/fetch.sh --from-host root@<t2>" >&2
        echo "             rootfs/fetch.sh --ota <url>" >&2
        exit 1
    fi
fi

if wanted 1; then
    echo "== 1/8 vendor firmware =="
    echo "  rootfs/firmware/: $(find rootfs/firmware -type f | wc -l) file(s)"
else
    echo "-- 1/8 vendor firmware: not selected"
fi

if wanted 2; then
    echo "== 2/8 initramfs =="
    # Stamped, not presence-checked: build/initramfs is embedded in the kernel
    # Image (step 4), so a changed init or installer.sh has to invalidate it.
    # The presence check used to leave a stale tree in place, and step 4 then
    # hashed that stale tree and skipped too.
    s2=$(step_hash "initramfs" rootfs/initramfs)
    if stamp_skip 2 initramfs step2-initramfs.sha256 "$s2" \
            build/initramfs/init build/initramfs/installer.sh \
            build/initramfs/bin/busybox; then
        :
    else
        rootfs/initramfs/build.sh
        stamp_write step2-initramfs.sha256 "$s2"
    fi
else
    echo "-- 2/8 initramfs: not selected"
fi

if wanted 3; then
    echo "== 3/8 kernel tree =="
    if [ -d build/kernel ]; then
        echo "  already present: build/kernel"
    else
        kernel/fetch.sh
    fi
else
    echo "-- 3/8 kernel tree: not selected"
fi

if wanted 4; then
    echo "== 4/8 kernel =="
    s4=$(step_hash "kernel $(git -C build/kernel rev-parse HEAD 2>/dev/null || echo -)" \
        kernel/build.sh kernel/fetch.sh kernel/config/kernel.config kernel/patches \
        build/initramfs)
    if stamp_skip 4 kernel step4-kernel.sha256 "$s4" \
            build/out/Image build/out/rk3568-t2.dtb build/out/modules/lib/modules; then
        :
    else
        kernel/build.sh
        stamp_write step4-kernel.sha256 "$s4"
    fi
else
    echo "-- 4/8 kernel: not selected"
fi

if wanted 5; then
    echo "== 5/8 u-boot sources =="
    u-boot/fetch.sh
else
    echo "-- 5/8 u-boot sources: not selected"
fi

if wanted 6; then
    echo "== 6/8 u-boot =="
    s6=$(step_hash "uboot $(git -C build/uboot rev-parse HEAD 2>/dev/null || echo -)" \
        u-boot/build.sh u-boot/fetch.sh u-boot/configs u-boot/dts u-boot/patches \
        u-boot/gen-installer-defconfig.py build/rkbin)
    if stamp_skip 6 u-boot step6-uboot.sha256 "$s6" \
            build/out/u-boot.itb build/out/idbloader.img \
            build/out/u-boot-installer.itb build/out/idbloader-installer.img \
            build/out/u-boot-initial-env build/out/u-boot-installer-initial-env; then
        :
    else
        u-boot/build.sh
        stamp_write step6-uboot.sha256 "$s6"
    fi
else
    echo "-- 6/8 u-boot: not selected"
fi

if wanted 7; then
    echo "== 7/8 rootfs =="
    s7=$(step_hash "rootfs" \
        rootfs/build.sh rootfs/fetch.sh rootfs/initramfs/build.sh rootfs/t2-distro.py \
        rootfs/profiles/t2-base rootfs/packages rootfs/firmware \
        rootfs/initramfs/firmware \
        lib/t2-build.py lib/rkimg.py images/rk-fit.py \
        build/out/Image build/out/rk3568-t2.dtb build/out/modules)
    if stamp_skip 7 rootfs step7-rootfs.sha256 "$s7" \
            build/out/rootfs.ext4 build/out/rootfs.ext4.zst; then
        :
    else
        rootfs/build.sh
        stamp_write step7-rootfs.sha256 "$s7"
    fi
else
    echo "-- 7/8 rootfs: not selected"
fi

if wanted 8; then
    echo "== 8/8 installer image =="
    s8_inputs=(images/build-installer.sh images/rk-fit.py images/t2-boot-fat.py \
        images/t2-image.py images/t2-install-card-config.example.txt \
        build/out/Image build/out/rk3568-t2.dtb build/out/u-boot.itb \
        build/out/idbloader.img build/out/rootfs.ext4.zst)
    if [ -f build/out/Image.old ]; then s8_inputs+=(build/out/Image.old); fi
    if [ -f build/out/u-boot-initial-env ]; then
        s8_inputs+=(build/out/u-boot-initial-env)
    fi
    s8=$(step_hash "installer" "${s8_inputs[@]}")
    if stamp_skip 8 installer step8-installer.sha256 "$s8" build/out/installer.img; then
        :
    else
        images/build-installer.sh
        stamp_write step8-installer.sha256 "$s8"
    fi
else
    echo "-- 8/8 installer image: not selected"
fi

echo
echo "build-all: done."
