#!/usr/bin/env bash
# Build the card-driven installer image for the ZSpace T2.
#
# The card carries three things:
#   * a T2-CONFIG FAT partition holding /t2-config.txt merged with the boot tree
#     as files (Image, the DTB, /extlinux/extlinux.conf and t2-emmc.conf,
#     /uboot.env, /Image.old) plus /u-boot.itb and /idbloader.img;
#   * a T2-FLASH ext4 partition holding /rootfs.ext4.zst;
#   * the vendor-layout boot chain at the eMMC offsets (loader at LBA 0x40,
#     U-Boot at LBA 0x4000, the mainline kernel FIT at LBA 0x8000).
#
# U-Boot's bootstd scans the card's FAT partition and selects the t2-installer
# entry (cmdline t2.mode=flash), so the initramfs runs its installer and writes
# the eMMC: new GPT, a fresh T2-BOOT p3, the rootfs, then U-Boot and the SPL.
#
# Inputs (all required, all under build/out/):
#   Image             rk3568-t2.dtb     u-boot.itb
#   idbloader.img     rootfs.ext4.zst
# Optional: u-boot-initial-env is added to the tree as /uboot.env whenever it
# exists, so the installed board keeps our default environment (bootdelay,
# preboot, bootcmd) instead of the vendor's; Image.old additionally adds the
# A/B fallback and arms /uboot.env's boot counter.  Both are opt-in and added
# automatically.
#
# Output: build/out/installer.img (write it to an SD card with `dd`).
#
# Usage:
#   images/build-installer.sh
#   images/build-installer.sh --config my-t2-config.txt
#
set -eu

SELF=$(readlink -f "$0")
HERE=$(dirname "$SELF")
ROOT=$(cd "$HERE/.." && pwd)
OUT="$ROOT/build/out"
BUILD="$ROOT/build"
PY=${PYTHON:-python3}

usage() {
	cat <<'EOF'
usage: images/build-installer.sh [--config FILE]

Assemble build/out/installer.img from the artifacts in build/out/:
  Image, rk3568-t2.dtb, u-boot.itb, idbloader.img, rootfs.ext4.zst.

  --config FILE  use FILE as the /t2-config.txt template instead of the
                 shipped images/t2-install-card-config.example.txt; the sha256
                 of rootfs.ext4.zst is substituted for <sha256 of the payload>.
  -h, --help     print this help
EOF
}

CONFIG=""
while [ $# -gt 0 ]; do
	case "$1" in
		--config) [ $# -ge 2 ] || { echo "build-installer: --config needs a file" >&2; exit 2; }
		          CONFIG=$2; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "build-installer: unknown argument: $1" >&2; exit 2 ;;
	esac
done

die() { echo "build-installer: $*" >&2; exit 1; }
log() { echo "build-installer: $*" >&2; }

for f in Image rk3568-t2.dtb u-boot.itb idbloader.img rootfs.ext4.zst; do
	[ -f "$OUT/$f" ] || die "missing $OUT/$f (run kernel/, u-boot/ and rootfs/ builds first)"
done
[ -f "$HERE/rk-fit.py" ] || die "missing $HERE/rk-fit.py"
[ -f "$HERE/t2-boot-fat.py" ] || die "missing $HERE/t2-boot-fat.py"
[ -f "$HERE/t2-image.py" ] || die "missing $HERE/t2-image.py"

# 1. the mainline kernel FIT (the vendor `boot` partition mirror)
log "packing the kernel FIT with rk-fit.py"
"$PY" "$HERE/rk-fit.py" --kernel "$OUT/Image" --dtb "$OUT/rk3568-t2.dtb" \
	--out "$OUT/t2-mainline-boot.img" || die "rk-fit.py failed"

# 2. the boot tree as files, for the card's FAT partition
TREE="$BUILD/installer-boot-tree"
PAYLOAD="$BUILD/installer-payload"
rm -rf "$TREE" "$PAYLOAD"
log "building the boot tree (t2-boot-fat.py --out-dir)"
boot_args=(--image "$OUT/Image" --dtb "$OUT/rk3568-t2.dtb" --out-dir "$TREE"
	--flash-append t2.mode=flash)
# /uboot.env carries the board's own compiled default environment; U-Boot reads
# its environment from the eMMC's FAT boot partition, so shipping it is what
# replaces whatever the vendor left there.  install.sh copies it into the new
# p3 when the tree has it.
if [ -f "$OUT/u-boot-initial-env" ]; then
	boot_args+=(--env-defaults "$OUT/u-boot-initial-env")
	log "  adding /uboot.env (the board's compiled default environment)"
else
	log "  no u-boot-initial-env: the tree ships without /uboot.env"
fi
# The A/B fallback needs a kernel to fall back to, and arming the boot counter
# is only safe with that kernel in the tree (altbootcmd fatloads /Image.old).
if [ -f "$OUT/Image.old" ]; then
	[ -f "$OUT/u-boot-initial-env" ] \
		|| die "$OUT/Image.old needs $OUT/u-boot-initial-env to arm the A/B boot counter"
	log "  adding the A/B fallback: /Image.old + armed /uboot.env"
	boot_args+=(--fallback-image "$OUT/Image.old")
else
	log "  no Image.old: the tree ships without the A/B fallback"
fi
"$PY" "$HERE/t2-boot-fat.py" "${boot_args[@]}" || die "t2-boot-fat.py failed"

# The installer reads the loaders from the tree when it writes the new eMMC
# p1/SPL, so copy them in next to the kernel and the DTB.
cp -f "$OUT/u-boot.itb" "$OUT/idbloader.img" "$TREE/" || die "copying the loaders failed"

# 3. the payload ext4 tree: only the rootfs image, decompressed by the installer
mkdir -p "$PAYLOAD"
cp -f "$OUT/rootfs.ext4.zst" "$PAYLOAD/rootfs.ext4.zst" || die "copying the payload failed"

# 4. /t2-config.txt: arm the flash flow and verify the payload sha256
CFG="$BUILD/t2-config.txt"
if [ -n "$CONFIG" ]; then
	[ -f "$CONFIG" ] || die "--config $CONFIG: no such file"
	log "using $CONFIG as /t2-config.txt"
	cp -f "$CONFIG" "$CFG"
else
	SHA=$(sha256sum "$OUT/rootfs.ext4.zst" | cut -d' ' -f1)
	log "building /t2-config.txt (flash.sha256=$SHA)"
	sed "s#<sha256 of the payload>#$SHA#" \
		"$HERE/t2-install-card-config.example.txt" > "$CFG"
	grep -q "^flash.sha256=$SHA\$" "$CFG" ||
		die "$HERE/t2-install-card-config.example.txt has no '<sha256 of the payload>' placeholder"
fi

# 5. assemble the whole-disk image
log "assembling $OUT/installer.img"
"$PY" "$HERE/t2-image.py" --out "$OUT/installer.img" --rootfs none \
	--size auto --config "$CFG" --config-size 256M \
	--boot-dir "$TREE" --payload-dir "$PAYLOAD" || die "t2-image.py failed"

log "done: $OUT/installer.img"
log "  $(sha256sum "$OUT/installer.img")"
log "  write it with: dd if=$OUT/installer.img of=/dev/sdX bs=4M conv=sparse"
