#!/usr/bin/env bash
#
# Build the ZSpace T2 kernel from the fetched mainline tree.
#
# Steps:
#   1. apply the five kernel/patches/*.patch to build/kernel (git am)
#   2. copy kernel/config/kernel.config over build/kernel/.config
#   3. make olddefconfig
#   4. make -jN Image dtbs modules
#   5. install Image, rk3568-t2.dtb and the module tree into build/out/
#
# Run kernel/fetch.sh first.  The script refuses to run when build/kernel is
# missing, or when it carries only some of the five patches.  A tree that
# already carries all five is rebuilt as it is, so build-all.sh can re-run this
# step after a failed or partial build.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)

tree=$repo/build/kernel
out=$repo/build/out
initramfs=$repo/build/initramfs
patchdir=$here/patches
cfg=$here/config/kernel.config

ARCH=${ARCH:-arm64}
CROSS_COMPILE=${CROSS_COMPILE-aarch64-linux-gnu-}
JOBS=${JOBS:-$(nproc)}

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help]

Apply the ZSpace T2 kernel patches to build/kernel, build Image + dtbs + modules
and install them into build/out/.

Environment:
  ARCH           kernel architecture       (default: $ARCH)
  CROSS_COMPILE  toolchain prefix          (default: $CROSS_COMPILE)
  JOBS           parallel make jobs        (default: nproc)
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

case ${1:-} in
    -h|--help)
        usage
        exit 0
        ;;
    '')
        ;;
    *)
        echo "error: unexpected argument: $1" >&2
        usage >&2
        exit 2
        ;;
esac

# --- preconditions ---------------------------------------------------------
if [ ! -f "$cfg" ]; then
    die "kernel config not found: $cfg"
fi

if ! git -C "$tree" rev-parse --git-dir >/dev/null 2>&1; then
    die "no Linux tree at $tree - run kernel/fetch.sh first"
fi

shopt -s nullglob
patches=("$patchdir"/000*.patch)
shopt -u nullglob
if [ ${#patches[@]} -eq 0 ]; then
    die "no patches found in $patchdir"
fi

# A tree that already carries every patch is not an error: build-all.sh re-runs
# this script after a failed or partial build, and re-applying the patches would
# fail.  A tree that carries only some of them is ambiguous, so it still stops.
applied=0
for p in "${patches[@]}"; do
    if git -C "$tree" apply --reverse --check "$p" >/dev/null 2>&1; then
        applied=$((applied + 1))
    fi
done
if [ "$applied" -gt 0 ] && [ "$applied" -lt "${#patches[@]}" ]; then
    die "$tree carries $applied of ${#patches[@]} patches; remove the tree and
     re-run kernel/fetch.sh for a clean build"
fi

if [ -n "$CROSS_COMPILE" ] && ! command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1; then
    die "${CROSS_COMPILE}gcc not found; install the $ARCH cross toolchain or set CROSS_COMPILE"
fi

# --- 1. apply patches ------------------------------------------------------
if [ "$applied" -eq "${#patches[@]}" ]; then
    echo "== the ${#patches[@]} patches are already applied to $tree; rebuilding them as they are =="
    echo "  $(git -C "$tree" log --oneline -"${#patches[@]}" | wc -l) commits in place"
else
    echo "== applying ${#patches[@]} patches =="
    git_flags=()
    # `git am` needs an identity to author the patch commits.  Use the kernel
    # clone's own if it has one; otherwise a neutral one, never a personal or
    # machine-specific address.
    git -C "$tree" config user.email >/dev/null 2>&1 || git_flags+=(-c user.email=t2-build@localhost)
    git -C "$tree" config user.name  >/dev/null 2>&1 || git_flags+=(-c user.name="T2 build")
    if ! git -C "$tree" "${git_flags[@]}" am "${patches[@]}"; then
        echo "error: git am failed. Inspect build/kernel and run" >&2
        echo "       'git -C build/kernel am --abort' to reset." >&2
        exit 1
    fi
    echo "  $(git -C "$tree" log --oneline -"${#patches[@]}" | wc -l) commits applied"
fi

# --- 2. install the curated config ----------------------------------------
echo "== kernel config =="
cp "$cfg" "$tree/.config"
echo "  copied $cfg -> $tree/.config"

# The committed config leaves CONFIG_INITRAMFS_SOURCE empty. The shipped Image
# embeds the initramfs tree, so point the symbol at this clone's own tree and
# refuse to build when that tree is missing rather than embed the wrong thing.
if [ ! -d "$initramfs" ]; then
    die "no initramfs at $initramfs - run rootfs/initramfs/build.sh first (the Image embeds it)"
fi
"$tree/scripts/config" --file "$tree/.config" \
    --set-str CONFIG_INITRAMFS_SOURCE "$initramfs"
echo "  CONFIG_INITRAMFS_SOURCE -> $initramfs"

# --- 3-4. configure and build ---------------------------------------------
make_opts=(-C "$tree" ARCH="$ARCH")
[ -n "$CROSS_COMPILE" ] && make_opts+=(CROSS_COMPILE="$CROSS_COMPILE")

echo "== olddefconfig =="
make "${make_opts[@]}" olddefconfig

echo "== building Image + dtbs + modules (-j$JOBS) =="
make "${make_opts[@]}" -j "$JOBS" Image dtbs modules

# --- 5. install artefacts --------------------------------------------------
echo "== installing into build/out =="
mkdir -p "$out"
cp -f "$tree/arch/arm64/boot/Image" "$out/Image"
cp -f "$tree/arch/arm64/boot/dts/rockchip/rk3568-t2.dtb" "$out/rk3568-t2.dtb"
rm -rf "$out/modules"
make "${make_opts[@]}" INSTALL_MOD_PATH="$out/modules" modules_install >/dev/null

echo "  $out/Image"
echo "  $out/rk3568-t2.dtb"
echo "  $out/modules/lib/modules/"
echo "done."
