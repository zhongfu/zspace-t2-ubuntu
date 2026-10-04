#!/usr/bin/env bash
#
# Build every artefact of this repository, in the order docs/building.md
# documents.
#
#   1  tools/components.py fetch  verify the pinned components, stage the raw files
#   2  rootfs/fetch.sh            verify the committed WiFi/BT firmware
#   3  rootfs/initramfs/build.sh  -> build/initramfs, build/initramfs.gz
#   4  rootfs/build.sh            -> build/out/rootfs.ext4.zst
#   5  images/build-installer.sh  -> build/out/installer.img
#
# This repository does not build the kernel, U-Boot or t2-utils.  Those live in
# zspace-t2-kernel, zspace-t2-bootloader and zspace-t2-ubuntu-utils, and step 1
# fetches the exact artefacts components.lock pins, sha256-verified: a component
# that does not match the lock stops the build here rather than producing an
# image that silently mixes versions.  Step 1 also stages the raw kernel and
# boot-chain files into build/out/, so the rootfs and installer steps keep
# reading every input from one directory.
#
# Re-runnable: steps 3, 4 and 5 record a stamp of their inputs under
# build/.stamps/ and print "[skip] ..." when a rerun hashes the same.  Step 3's
# inputs are rootfs/initramfs/ (its packed output is the boot FIT's ramdisk);
# step 4's are the profile tree, the fetched component artefacts and the
# firmware trees; step 5's are the staged artefacts, the boot-tree tools and the
# config template.
#
# Steps can be selected: --only, --skip, --from, --to, --list.  For example,
# `--only 5` rebuilds the installer image only.
#
# Step 2 verifies the vendor WiFi/BT firmware committed in rootfs/firmware/brcm/
# and writes the mainline names; it needs no network.  Step 4 runs it too, so a
# step-4-only run is covered.  This script reports a missing blob instead of
# guessing.
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
  --only N[,N]   run only these steps (1..5); leave the rest alone
  --skip N[,N]   do not run these steps
  --from N       start at step N (inclusive)
  --to N         stop after step N (inclusive)
  --list         list the steps and exit
  -h, --help     show this help

Steps:
  1  components           tools/components.py fetch     (sha256-verified)
  2  vendor firmware      rootfs/fetch.sh               (verifies the committed blobs)
  3  initramfs            rootfs/initramfs/build.sh     (stamped)
  4  rootfs               rootfs/build.sh               (stamped)
  5  installer image      images/build-installer.sh     (stamped)

Steps 3, 4 and 5 are skipped when the sha256 of their inputs matches the stamp
under build/.stamps/.

Environment:
  JOBS                   build parallelism (default: \$(nproc))
  T2_COMPONENTS_DIR      take the component artefacts from this directory
                         instead of the component releases (offline builds)
  T2_COMPONENTS_ORG      GitHub org owning the component repositories
EOF
}

list_steps() {
    cat <<EOF
1  components           tools/components.py fetch
2  vendor firmware      rootfs/fetch.sh
3  initramfs            rootfs/initramfs/build.sh
4  rootfs               rootfs/build.sh
5  installer image      images/build-installer.sh
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
        [1-5]) ;;
        *) echo "build-all: $1 '$n' is not a step number (1..5)" >&2; exit 2 ;;
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
        echo "[skip] $n/5 $label: unchanged ($stamp)"
        return 0
    fi
    return 1
}

stamp_write() { # <stamp name> <hash>
    mkdir -p "$stamp_dir"
    printf '%s\n' "$2" > "$stamp_dir/$1"
}

# stage <component> <file>... - copy a fetched component artefact into
# build/out/, where the rootfs and installer steps read it.
stage() {
    local comp=$1
    shift
    local f
    for f in "$@"; do
        install -m 644 "build/components/$comp/$f" "build/out/$f"
    done
}

selected=
for n in 1 2 3 4 5; do
    if wanted "$n"; then selected="$selected $n"; fi
done
echo "build-all: steps$selected (of 1..5)"

export JOBS=${JOBS:-$(nproc)}

if wanted 1; then
    echo "== 1/5 components =="
    python3 tools/components.py fetch
    # Stage the raw files the later steps read.  The kernel package and the
    # bootloader package are the source of truth for these bytes; build/out/ is
    # only where this build puts them so every step reads one directory.
    stage kernel Image rk3568-t2.dtb
    stage bootloader u-boot.itb idbloader.img u-boot-installer.itb \
        idbloader-installer.img u-boot-initial-env u-boot-installer-initial-env
    # Unpack the userspace package: t2-mkfit lives in it, and both the rootfs
    # step (refreshing a stale FIT) and the installer step (packing it) need the
    # tool on disk.  One unpack here beats two copies of the same logic.
    utils_deb=$(ls build/components/utils/t2-utils_*.deb)
    rm -rf build/components/utils/unpacked
    mkdir -p build/components/utils/unpacked
    dpkg-deb -x "$utils_deb" build/components/utils/unpacked
    [ -x build/components/utils/unpacked/usr/bin/t2-mkfit ] || {
        echo "build-all: $utils_deb carries no /usr/bin/t2-mkfit" >&2
        exit 1
    }
else
    echo "-- 1/5 components: not selected"
fi

if wanted 2; then
    echo "== 2/5 vendor firmware =="
    rootfs/fetch.sh
else
    echo "-- 2/5 vendor firmware: not selected"
fi

if wanted 3; then
    echo "== 3/5 initramfs =="
    # Stamped, not presence-checked: the packed output is the boot FIT's ramdisk
    # and the t2-initramfs package's payload, so a changed init or installer.sh
    # has to invalidate it.
    s3=$(step_hash "initramfs" rootfs/initramfs)
    if stamp_skip 3 initramfs step3-initramfs.sha256 "$s3" \
            build/initramfs/init build/initramfs/installer.sh \
            build/initramfs/bin/busybox build/initramfs.gz; then
        :
    else
        rootfs/initramfs/build.sh
        stamp_write step3-initramfs.sha256 "$s3"
    fi
else
    echo "-- 3/5 initramfs: not selected"
fi

if wanted 4; then
    echo "== 4/5 rootfs =="
    # The component artefacts are inputs: the rootfs installs the fetched
    # t2-utils package, the kernel's module package and the initramfs it packs
    # here, so a component bump must rebuild the image.
    s4=$(step_hash "rootfs" \
        rootfs/build.sh rootfs/fetch.sh rootfs/initramfs/build.sh \
        rootfs/initramfs/package.sh rootfs/t2-distro.py \
        rootfs/profiles/t2-base rootfs/firmware rootfs/initramfs/firmware \
        lib/t2lib.py lib/rkimg.py images/t2-boot-fat.py \
        build/components build/initramfs.gz)
    if stamp_skip 4 rootfs step4-rootfs.sha256 "$s4" \
            build/out/rootfs.ext4 build/out/rootfs.ext4.zst; then
        :
    else
        rootfs/build.sh
        stamp_write step4-rootfs.sha256 "$s4"
    fi
else
    echo "-- 4/5 rootfs: not selected"
fi

if wanted 5; then
    echo "== 5/5 installer image =="
    s5_inputs=(images/build-installer.sh images/t2-boot-fat.py images/t2-image.py \
        images/t2-install-card-config.example.txt \
        build/out/Image build/out/rk3568-t2.dtb build/out/u-boot.itb \
        build/out/idbloader.img build/out/rootfs.ext4.zst build/initramfs.gz)
    if [ -f build/out/Image.old ]; then s5_inputs+=(build/out/Image.old); fi
    if [ -f build/out/u-boot-initial-env ]; then
        s5_inputs+=(build/out/u-boot-initial-env)
    fi
    s5=$(step_hash "installer" "${s5_inputs[@]}")
    if stamp_skip 5 installer step5-installer.sha256 "$s5" build/out/installer.img; then
        :
    else
        images/build-installer.sh
        stamp_write step5-installer.sha256 "$s5"
    fi
else
    echo "-- 5/5 installer image: not selected"
fi

echo
echo "build-all: done."
