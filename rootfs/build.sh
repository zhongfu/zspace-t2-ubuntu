#!/usr/bin/env bash
#
# Build the Ubuntu rootfs for the ZSpace T2.
#
# Steps:
#   1. make sure the vendor AP6275P firmware is present (run rootfs/fetch.sh)
#   2. build the bring-up initramfs into build/initramfs when it is missing
#   3. stage the firmware tree the profile's hooks read (build/firmware)
#   4. run rootfs/t2-distro.py -> build/rootfs/rootfs.ext4 (+ manifest)
#   5. install build/out/rootfs.ext4 and zstd-compress it to rootfs.ext4.zst
#
# The zstd copy is what images/build-installer.sh puts into the installer
# payload (T2-FLASH/rootfs.ext4.zst).
#
# Build order: firmware fetch -> initramfs -> kernel -> u-boot -> rootfs ->
# installer image.  This script needs firmware and (unless --dry-run) the
# built kernel tree, so run kernel/fetch.sh and kernel/build.sh first.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)

profile=$here/profiles/t2-base
work=$repo/build/rootfs
out=$repo/build/out
kernel_tree=$repo/build/kernel
initramfs=$repo/build/initramfs
fit=$out/t2-mainline-boot.img

jobs=${JOBS:-$(nproc)}
dry=0
zstd_compress=1
no_fit_check=0
rebuild_initramfs=0
fw_args=()
force_firmware=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] [options]

Build the ZSpace T2 Ubuntu rootfs.

Options:
  --dry-run                print the plan; run no build step
  --no-zstd                do not write build/out/rootfs.ext4.zst
  --rebuild-initramfs      rebuild build/initramfs even when it exists
  --no-fit-check           skip the boot-FIT freshness check (automatic when
                           build/out/t2-mainline-boot.img does not exist)
  --firmware-from-host H   fetch missing firmware from a T2 over SSH
  --firmware-ota URL       fetch missing firmware from a .zspace OTA package
  --firmware-from-dir DIR  fetch missing firmware from a local directory
  --force-firmware         re-run the firmware fetch even when present
  -j, --jobs N             make/apt jobs (default: $jobs)
  -h, --help               show this help

Environment:
  JOBS                     parallel jobs (default: $jobs)
EOF
}

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)              usage; exit 0 ;;
        --dry-run)              dry=1; shift ;;
        --no-zstd)              zstd_compress=0; shift ;;
        --rebuild-initramfs)    rebuild_initramfs=1; shift ;;
        --no-fit-check)         no_fit_check=1; shift ;;
        --force-firmware)       force_firmware=1; shift ;;
        --firmware-from-host)   fw_args+=(--from-host "$2"); shift 2 ;;
        --firmware-ota)         fw_args+=(--ota "$2"); shift 2 ;;
        --firmware-from-dir)    fw_args+=(--from-dir "$2"); shift 2 ;;
        -j|--jobs)              jobs=$2; shift 2 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required"
[ -d "$profile" ] || die "profile $profile is missing"

# ------------------------------------------------------------ 1. firmware
fw_ok() {
    local f
    for f in fw_bcm43752a2_pcie_ag.bin clm_bcm43752a2_ag.blob \
             nvram_AP6275P.txt BCM4362A2.hcd; do
        [ -f "$here/firmware/brcm/$f" ] || return 1
    done
    return 0
}

if [ "$dry" = 0 ] && ! fw_ok; then
    if [ ${#fw_args[@]} -gt 0 ]; then
        echo "== firmware missing; fetching =="
        "$here/fetch.sh" "${fw_args[@]}"
    else
        die "vendor firmware missing from rootfs/firmware/brcm/; run
     rootfs/fetch.sh --from-host root@<t2>   (or --ota <url>, --from-dir <dir>)"
    fi
fi
if [ "$force_firmware" = 1 ] && [ ${#fw_args[@]} -gt 0 ] && [ "$dry" = 0 ]; then
    "$here/fetch.sh" --force "${fw_args[@]}"
fi

# ------------------------------------------------------------ 2. initramfs
if [ "$dry" = 1 ]; then
    echo "[dry] would build the initramfs into $initramfs (rootfs/initramfs/build.sh)"
elif [ "$rebuild_initramfs" = 1 ] || [ ! -f "$initramfs/init" ]; then
    echo "== building the initramfs =="
    "$here/initramfs/build.sh" --out "$initramfs" -j "$jobs"
else
    echo "== initramfs already present: $initramfs (use --rebuild-initramfs to redo) =="
fi

# ------------------------------------------------------------ 3. firmware tree
# The profile's image.json names build/firmware as firmware_src; hooks/50 reads
# firmware/brcm and firmware/rtl_nic from there.
if [ "$dry" = 1 ]; then
    echo "[dry] would stage $repo/build/firmware (brcm + rtl_nic + regulatory)"
else
    echo "== staging the firmware tree =="
    rm -rf "$repo/build/firmware"
    mkdir -p "$repo/build/firmware/brcm" "$repo/build/firmware/rtl_nic"
    cp -f "$here/firmware/brcm/"* "$repo/build/firmware/brcm/"
    cp -f "$here/initramfs/firmware/rtl_nic/"*.fw \
          "$repo/build/firmware/rtl_nic/"
    cp -f "$here/initramfs/firmware/regulatory.db" \
          "$here/initramfs/firmware/regulatory.db.p7s" "$repo/build/firmware/"
fi

# ------------------------------------------------------------ 4. rootfs image
[ -d "$kernel_tree" ] || { [ "$dry" = 1 ] || die \
    "kernel tree $kernel_tree is missing; run kernel/fetch.sh && kernel/build.sh"; }

# The FIT is built later by images/ (build/out/t2-mainline-boot.img).  Only
# check it when it is there; a rootfs-only build must not fail on it.
t2_args=(--profile "$profile" --out "$work" --kernel-tree "$kernel_tree" -j "$jobs")
if [ "$no_fit_check" = 1 ]; then
    t2_args+=(--no-fit-check)
elif [ ! -f "$fit" ]; then
    t2_args+=(--no-fit-check)
    echo "note: $fit is absent (images/ builds it later); skipping the FIT check"
fi
[ "$dry" = 1 ] && t2_args+=(--dry-run)

echo "== building the rootfs image =="
python3 "$here/t2-distro.py" "${t2_args[@]}"

# ------------------------------------------------------------ 5. install
if [ "$dry" = 1 ]; then
    echo "[dry] would install $work/rootfs.ext4 -> $out/rootfs.ext4"
    [ "$zstd_compress" = 1 ] && echo "[dry] would zstd-compress it to rootfs.ext4.zst"
    exit 0
fi

[ -f "$work/rootfs.ext4" ] || die "t2-distro did not produce $work/rootfs.ext4"
mkdir -p "$out"
install -m 644 "$work/rootfs.ext4" "$out/rootfs.ext4"
echo "  $out/rootfs.ext4"

if [ "$zstd_compress" = 1 ]; then
    command -v zstd >/dev/null 2>&1 || die "zstd is required to write
     build/out/rootfs.ext4.zst (images/build-installer.sh needs it); install
     zstd or pass --no-zstd"
    zstd -1 -T0 -q -f "$out/rootfs.ext4" -o "$out/rootfs.ext4.zst"
    echo "  $out/rootfs.ext4.zst"
fi

echo "done."
