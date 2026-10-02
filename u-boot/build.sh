#!/bin/sh
# Build mainline U-Boot for the ZSpace T2 (RK3568).
#
# Run u-boot/fetch.sh first.  This script installs the board defconfigs and the
# two board device trees into build/uboot, applies u-boot/patches/*, and builds
# two images:
#   u-boot.itb            the *plain* image for the eMMC (boots the eMMC boot
#                         tree; a held power button selects the card)
#   u-boot-installer.itb  the *installer* image for the card's loader partition
#                         (boots the card's installer tree directly)
# plus the matching idbloader.img for each and the initial-environment text
# files.  Everything lands in build/out/.
#
# The defconfig installs the full board configuration - see u-boot/README.md:
# the T2 control DT, the power-on gate/LEDs, the env in the FAT boot partition
# (`:3`) with a bootcount A/B fallback, the rockusb flash workflow.  The
# installer defconfig is derived from the plain one on every build.
#
# Reproducibility: SOURCE_DATE_EPOCH is pinned to the U-Boot commit's time, so
# two clean builds produce bit-identical artefacts (U-Boot otherwise embeds the
# wall-clock build time).
#
# Usage: u-boot/build.sh [-h]
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)

BUILD=${BUILD:-$ROOT/build}
UBOOT=${UBOOT:-$BUILD/uboot}
RKBIN=${RKBIN:-$BUILD/rkbin}
OUT=${OUT:-$BUILD/out}
JOBS=${JOBS:-$(nproc)}

BL31=${BL31:-$RKBIN/bin/rk35/rk3568_bl31_v1.46.elf}
ROCKCHIP_TPL=${ROCKCHIP_TPL:-$RKBIN/bin/rk35/rk3568_ddr_1560MHz_v1.26.bin}

usage() {
	cat <<EOF
usage: u-boot/build.sh

Build the ZSpace T2 U-Boot images into $OUT.
Environment overrides: UBOOT, RKBIN, OUT, JOBS, CROSS_COMPILE, BL31,
ROCKCHIP_TPL.
EOF
}

case ${1:-} in
-h|--help) usage; exit 0 ;;
"") ;;
*) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
esac

if [ ! -d "$UBOOT" ]; then
	echo "error: $UBOOT not found - run u-boot/fetch.sh first" >&2
	exit 1
fi

# --- host toolchain ---------------------------------------------------------
if [ -z "${CROSS_COMPILE:-}" ]; then
	if command -v aarch64-linux-gnu-gcc >/dev/null 2>&1; then
		CROSS_COMPILE=aarch64-linux-gnu-
	else
		echo "error: no aarch64 cross compiler on PATH." >&2
		echo "       install gcc-aarch64-linux-gnu or set CROSS_COMPILE." >&2
		exit 1
	fi
fi
export CROSS_COMPILE

# binman's python module needs swig (see patches/0001-dtc-pylibfdt-swig4.patch).
if ! command -v swig >/dev/null 2>&1; then
	echo "error: swig is not on PATH (binman needs the python libfdt module)." >&2
	echo "       install swig and re-run." >&2
	exit 1
fi

for f in "$BL31" "$ROCKCHIP_TPL"; do
	if [ ! -f "$f" ]; then
		echo "error: missing blob $f - run u-boot/fetch.sh first," >&2
		echo "       or set BL31 / ROCKCHIP_TPL to the rkbin paths." >&2
		exit 1
	fi
done
export BL31 ROCKCHIP_TPL

echo "u-boot: tree=$UBOOT"
echo "u-boot: CROSS_COMPILE=$CROSS_COMPILE JOBS=$JOBS"
echo "u-boot: BL31=$BL31"
echo "u-boot: ROCKCHIP_TPL=$ROCKCHIP_TPL"

# --- install board sources and patches --------------------------------------
cp "$HERE/configs/t2-rk3568_defconfig" "$UBOOT/configs/t2-rk3568_defconfig"
cp "$HERE/dts/rk3568-t2.dts" "$UBOOT/dts/upstream/src/arm64/rockchip/rk3568-t2.dts"
cp "$HERE/dts/rk3568-t2-u-boot.dtsi" "$UBOOT/arch/arm/dts/rk3568-t2-u-boot.dtsi"

for p in "$HERE"/patches/*.patch; do
	[ -e "$p" ] || continue
	if git -C "$UBOOT" apply --reverse --check "$p" >/dev/null 2>&1; then
		echo "u-boot: patch already applied: $(basename "$p")"
	elif git -C "$UBOOT" apply --check "$p" >/dev/null 2>&1; then
		echo "u-boot: applying $(basename "$p")"
		git -C "$UBOOT" apply "$p"
	else
		echo "error: patch does not apply: $p" >&2
		exit 1
	fi
done

# --- reproducible build timestamp -------------------------------------------
SOURCE_DATE_EPOCH=$(git -C "$UBOOT" log -1 --format=%ct)
export SOURCE_DATE_EPOCH

CONFIG=$UBOOT/scripts/config
mkdir -p "$OUT"

cd "$UBOOT"

# The installer defconfig is derived from the plain one that was just
# installed.  See gen-installer-defconfig.py for why it is generated.
python3 "$HERE/gen-installer-defconfig.py" "$UBOOT"

# --- installer image (the card's loader partition) --------------------------
make t2-rk3568-installer_defconfig
"$CONFIG" --disable TOOLS_MKEFICAPSULE
make olddefconfig
make -j"$JOBS"
cp u-boot.itb "$OUT/u-boot-installer.itb"
cp idbloader.img "$OUT/idbloader-installer.img"
# `make u-boot-initial-env` prints the defaults through the host tool
# tools/printinitialenv, which compiles include/env_default.h - including the
# CONFIG_BOOTCOMMAND from include/generated/autoconf.h.  The make rule does not
# depend on .config, so the tool is not rebuilt after a defconfig change and
# the env text silently keeps the previous bootcmd.  Delete it first.
rm -f tools/printinitialenv tools/.printinitialenv.cmd
make u-boot-initial-env
cp u-boot-initial-env "$OUT/u-boot-installer-initial-env"

# --- plain image (the eMMC) -------------------------------------------------
make t2-rk3568_defconfig
"$CONFIG" --disable TOOLS_MKEFICAPSULE
make olddefconfig
make -j"$JOBS"
cp u-boot.itb "$OUT/u-boot.itb"
cp idbloader.img "$OUT/idbloader.img"
rm -f tools/printinitialenv tools/.printinitialenv.cmd
make u-boot-initial-env
cp u-boot-initial-env "$OUT/u-boot-initial-env"

echo
echo "built $OUT/u-boot.itb ($(stat -c %s "$OUT/u-boot.itb") bytes)                       [plain, eMMC]"
echo "built $OUT/idbloader.img ($(stat -c %s "$OUT/idbloader.img") bytes)"
echo "built $OUT/u-boot-installer.itb ($(stat -c %s "$OUT/u-boot-installer.itb") bytes)   [card loader]"
echo "built $OUT/idbloader-installer.img ($(stat -c %s "$OUT/idbloader-installer.img") bytes)"
echo "env   $OUT/u-boot-initial-env ($(stat -c %s "$OUT/u-boot-initial-env") bytes)"
echo "env   $OUT/u-boot-installer-initial-env ($(stat -c %s "$OUT/u-boot-installer-initial-env") bytes)"
