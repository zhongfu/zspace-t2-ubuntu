#!/usr/bin/env bash
#
# Fetch the vendor (non-redistributable) AP6275P firmware for the ZSpace T2.
#
# The Broadcom/Cypress BCM43752 blobs are not in linux-firmware and may not be
# redistributed, so they are not in this repository.  This script copies them
# into rootfs/firmware/brcm/ from a T2 that still runs the vendor firmware, from
# a vendor .zspace update/OTA package, or from a directory that already holds
# them, and verifies every SHA-256 (rootfs/firmware/README.md lists them).
#
# It also writes the mainline brcmfmac names (brcmfmac43752-pcie.{bin,txt,clm_blob}),
# so both the rootfs hooks and the embedded initramfs find what they ask for.
#
# Run it before the initramfs and rootfs builds:
#   rootfs/fetch.sh --from-host root@192.168.1.50
#   rootfs/fetch.sh --ota https://example.invalid/t2/update.zspace
#   rootfs/fetch.sh --from-dir /mnt/vendor-rootfs/system/etc/firmware
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)

dest=$repo/rootfs/firmware
src_dir=/system/etc/firmware
force=0

mode=""
arg_host=""
arg_ota=""
arg_dir=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] --from-host HOST | --ota URL | --from-dir DIR

Copy and verify the vendor AP6275P firmware into rootfs/firmware/brcm/.

Options:
  --from-host [user@]HOST  copy from a T2 that runs the vendor firmware over SSH
  --ota URL                read the files from a vendor .zspace OTA package
  --from-dir DIR           copy from a directory that already holds them
  --src-dir DIR            firmware directory on the device (default: $src_dir)
  --dest DIR               destination root (default: $dest)
  --force                  re-copy even when a verified file is already there
  -h, --help               show this help

Sources may be combined; the files are merged and each is verified separately.
EOF
}

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)   usage; exit 0 ;;
        --from-host) mode="$mode host"; arg_host=$2; shift 2 ;;
        --ota)       mode="$mode ota";  arg_ota=$2;  shift 2 ;;
        --from-dir)  mode="$mode dir";  arg_dir=$2;  shift 2 ;;
        --src-dir)   src_dir=$2; shift 2 ;;
        --dest)      dest=$2; shift 2 ;;
        --force)     force=1; shift ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

if [ -z "$mode" ]; then
    usage
    exit 2
fi

# filename -> sha256 (rootfs/firmware/README.md)
files=(fw_bcm43752a2_pcie_ag.bin clm_bcm43752a2_ag.blob nvram_AP6275P.txt BCM4362A2.hcd)
declare -A sha=(
    [fw_bcm43752a2_pcie_ag.bin]=6a2dbe01e72221defba91a52e158768d973a3c85ca2d881c924379e35ad36b23
    [clm_bcm43752a2_ag.blob]=5143146e1923f87f7aab8df043abcf89a657fa9fdc3b22a38806399730d9a97a
    [nvram_AP6275P.txt]=b4780f7b86a5680dbe496815dbe07aa22010701de2d8c60570b8274a42db215b
    [BCM4362A2.hcd]=18901a5bef1d418b6895e92d0afae36234f4160b237465dfca3d75e9844e93ef
)

hash_of() { sha256sum "$1" | awk '{print $1}'; }

# Every file already verified in dest/brcm/?  Then there is nothing to fetch
# unless --force says to refresh anyway.
missing=0
for f in "${files[@]}"; do
    if [ ! -f "$dest/brcm/$f" ] || [ "$(hash_of "$dest/brcm/$f")" != "${sha[$f]}" ]; then
        missing=1
    fi
done
if [ "$missing" = 0 ] && [ "$force" = 0 ]; then
    echo "rootfs/fetch: firmware already present and verified in $dest/brcm/ - nothing to do"
    echo "              (use --force to refresh)"
    exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/got" "$dest/brcm"

for m in $mode; do
    case "$m" in
        host)
            echo "== fetching from $arg_host:$src_dir =="
            for f in "${files[@]}"; do
                [ -f "$tmp/got/$f" ] && continue
                scp -q -o BatchMode=yes "$arg_host:$src_dir/$f" "$tmp/got/$f" \
                    || die "scp $arg_host:$src_dir/$f failed"
                echo "  got $f"
            done
            ;;
        dir)
            echo "== copying from $arg_dir =="
            for f in "${files[@]}"; do
                [ -f "$tmp/got/$f" ] && continue
                [ -f "$arg_dir/$f" ] || die "$arg_dir/$f is missing"
                cp -f "$arg_dir/$f" "$tmp/got/$f"
                echo "  got $f"
            done
            ;;
        ota)
            ota_tool=$here/zspace-ota.py
            [ -f "$ota_tool" ] || die "$ota_tool is missing"
            echo "== extracting from $arg_ota =="
            pat='(fw_bcm43752a2_pcie_ag\.bin|clm_bcm43752a2_ag\.blob|nvram_AP6275P\.txt|BCM4362A2\.hcd)$'
            python3 "$ota_tool" get "$arg_ota" --out "$tmp/ota" --grep "$pat" \
                || die "OTA extraction failed"
            for f in "${files[@]}"; do
                [ -f "$tmp/got/$f" ] && continue
                found=$(find "$tmp/ota" -type f -name "$f" -print -quit)
                [ -n "$found" ] || die "$f not found in the OTA package"
                cp -f "$found" "$tmp/got/$f"
                echo "  got $f"
            done
            ;;
    esac
done

echo "== verifying and installing =="
for f in "${files[@]}"; do
    [ -f "$tmp/got/$f" ] || die "$f was not obtained from any source"
    got=$(hash_of "$tmp/got/$f")
    if [ "$got" != "${sha[$f]}" ]; then
        die "$f sha256 $got != expected ${sha[$f]} (wrong vendor build?)"
    fi
    echo "  $f sha256 ok"
    cp -f "$tmp/got/$f" "$dest/brcm/$f"
done

# Driver-named copies mainline brcmfmac requests (rootfs/firmware/README.md).
cp -f "$dest/brcm/fw_bcm43752a2_pcie_ag.bin" "$dest/brcm/brcmfmac43752-pcie.bin"
cp -f "$dest/brcm/nvram_AP6275P.txt"         "$dest/brcm/brcmfmac43752-pcie.txt"
cp -f "$dest/brcm/clm_bcm43752a2_ag.blob"    "$dest/brcm/brcmfmac43752-pcie.clm_blob"
chmod 644 "$dest/brcm/"*

echo
echo "firmware installed:"
ls -l "$dest/brcm/"
echo "done."
