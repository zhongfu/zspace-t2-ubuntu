#!/bin/sh
# Fetch the sources U-Boot for the ZSpace T2 needs:
#   build/uboot  - upstream U-Boot v2026.07 (git clone)
#   build/rkbin  - rockchip-linux/rkbin, sparse, only bin/rk35
#
# The rkbin tree supplies the two closed-source blobs binman must pack into the
# FIT and the idbloader:
#   rk3568_bl31_v1.46.elf        BL31 (ATF: PSCI secure monitor)
#   rk3568_ddr_1560MHz_v1.26.bin  TPL (SRAM DDR init/training; RK3568 U-Boot
#                                  does not init DRAM itself)
#
# Usage: u-boot/fetch.sh [-h]
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)

BUILD=${BUILD:-$ROOT/build}
UBOOT_SRC=${UBOOT_SRC:-$BUILD/uboot}
RKBIN_SRC=${RKBIN_SRC:-$BUILD/rkbin}

UBOOT_REPO=${UBOOT_REPO:-https://github.com/u-boot/u-boot.git}
UBOOT_TAG=${UBOOT_TAG:-v2026.07}
RKBIN_REPO=${RKBIN_REPO:-https://github.com/rockchip-linux/rkbin.git}
# Pinned rkbin revision: the blob versions measured.  rkbin's default branch
# HEAD is this commit today.
RKBIN_REF=${RKBIN_REF:-3e288fe814e059dd06833495f845cab04ac20a5c}

usage() {
	cat <<EOF
usage: u-boot/fetch.sh

Fetch U-Boot $UBOOT_TAG into $UBOOT_SRC and the rkbin blobs into $RKBIN_SRC.
Override with the environment variables UBOOT_SRC, RKBIN_SRC, BUILD,
UBOOT_REPO, UBOOT_TAG, RKBIN_REPO, RKBIN_REF.
EOF
}

case ${1:-} in
-h|--help) usage; exit 0 ;;
"") ;;
*) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
esac

mkdir -p "$BUILD"

if [ -d "$UBOOT_SRC/.git" ]; then
	echo "u-boot: $UBOOT_SRC already exists, skipping clone"
else
	echo "u-boot: cloning $UBOOT_REPO ($UBOOT_TAG) -> $UBOOT_SRC"
	git clone --depth 1 --branch "$UBOOT_TAG" "$UBOOT_REPO" "$UBOOT_SRC"
fi

if [ -d "$RKBIN_SRC/.git" ]; then
	echo "rkbin: $RKBIN_SRC already exists, skipping clone"
else
	# Blobless sparse clone: only bin/rk35 is downloaded.
	echo "rkbin: cloning $RKBIN_REPO (sparse: bin/rk35) -> $RKBIN_SRC"
	git clone --depth 1 --filter=blob:none --sparse "$RKBIN_REPO" "$RKBIN_SRC"
	git -C "$RKBIN_SRC" sparse-checkout set bin/rk35
fi

if [ -n "${RKBIN_REF:-}" ] && [ "$(git -C "$RKBIN_SRC" rev-parse HEAD)" != "$RKBIN_REF" ]; then
	echo "rkbin: checking out $RKBIN_REF"
	git -C "$RKBIN_SRC" fetch --depth 1 origin "$RKBIN_REF"
	git -C "$RKBIN_SRC" checkout --detach "$RKBIN_REF"
fi

BL31=$RKBIN_SRC/bin/rk35/rk3568_bl31_v1.46.elf
TPL=$RKBIN_SRC/bin/rk35/rk3568_ddr_1560MHz_v1.26.bin
for f in "$BL31" "$TPL"; do
	if [ ! -f "$f" ]; then
		echo "error: rkbin file missing: $f" >&2
		exit 1
	fi
done

echo "fetched U-Boot $(git -C "$UBOOT_SRC" describe --tags --always) at $UBOOT_SRC"
echo "fetched rkbin $(git -C "$RKBIN_SRC" rev-parse --short HEAD) at $RKBIN_SRC"
echo "  BL31=$BL31 ($(stat -c %s "$BL31") bytes)"
echo "  ROCKCHIP_TPL=$TPL ($(stat -c %s "$TPL") bytes)"
