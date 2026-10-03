#!/usr/bin/env bash
#
# Collect the release assets from a finished build into one directory, and
# write the file list for the release notes.
#
# Usage: tools/collect-release-artifacts.sh [options] [SRC] [DEST]
#
#   SRC             build output directory       (default: build/out)
#   DEST            staging directory for assets (default: dist/release)
#   --src DIR       same as the first positional argument
#   --dest DIR      same as the second positional argument
#   --notes FILE    also write the "files" section of the release notes
#   -h, --help
#
# SRC is build/out after a full build (build-all.sh).  The t2-utils Debian
# package is found by glob, never by a hard-coded path, so wherever the rootfs
# build puts it CI finds it.  Every required asset must be there: the script
# fails loudly instead of publishing an incomplete release.
#
# The release carries a SHA256SUMS file it generates itself; run
# `sha256sum -c SHA256SUMS` in the download directory to check it.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)

src=$root/build/out
dest=$root/dist/release
notes=

usage() {
    cat <<'EOF'
Collect the release assets from a finished build.

Usage: tools/collect-release-artifacts.sh [options] [SRC] [DEST]
  SRC           build output directory        (default: build/out)
  DEST          staging directory for assets  (default: dist/release)
  --src DIR     same as the first positional argument
  --dest DIR    same as the second positional argument
  --notes FILE  write the release-notes "files" section to FILE
  -h, --help    this text
EOF
}

pos=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --src)  src=$2; shift 2 ;;
        --dest) dest=$2; shift 2 ;;
        --notes) notes=$2; shift 2 ;;
        -*) echo "collect-release-artifacts: unknown option: $1" >&2; exit 2 ;;
        *)
            pos=$((pos + 1))
            if [ "$pos" = 1 ]; then src=$1
            elif [ "$pos" = 2 ]; then dest=$1
            else echo "collect-release-artifacts: too many arguments" >&2; exit 2
            fi
            shift
            ;;
    esac
done

[ -d "$src" ] || { echo "error: source directory $src does not exist" >&2; exit 1; }

# --------------------------------------------------------------------------
# The release assets.  One source of truth for what is copied and for the
# release-notes table, so the two can never drift apart.
# --------------------------------------------------------------------------
# Every release carries these; the script fails without them.
required=(
    installer.img
    rootfs.ext4.zst
    Image
    rk3568-t2.dtb
    t2-mainline-boot.img
    u-boot.itb
    idbloader.img
    u-boot-installer.itb
    idbloader-installer.img
    u-boot-initial-env
    u-boot-installer-initial-env
)
# Carried when the build produced them (not every build does).
optional=(
    Image.old
)

declare -A desc=(
    [installer.img]="Write this SD-card installer image to a card and boot the board. It installs the system to the eMMC. This is the file most users want."
    [rootfs.ext4.zst]="The Ubuntu root filesystem, zstd-compressed. The installer writes it to the eMMC."
    [Image]="The Linux kernel image."
    [rk3568-t2.dtb]="The device tree blob for the T2 board."
    [t2-mainline-boot.img]="The kernel FIT: kernel, device tree and initramfs in one bootable image. U-Boot loads this."
    [u-boot.itb]="U-Boot bootloader for the eMMC."
    [idbloader.img]="Rockchip loader for the eMMC: DDR init and the U-Boot SPL."
    [u-boot-installer.itb]="U-Boot bootloader for the SD-card installer."
    [idbloader-installer.img]="Rockchip loader for the SD-card installer: DDR init and the U-Boot SPL."
    [u-boot-initial-env]="The U-Boot default environment for the eMMC image."
    [u-boot-installer-initial-env]="The U-Boot default environment for the SD-card installer."
    [Image.old]="The previous kernel image, kept for the U-Boot A/B fallback."
    [SHA256SUMS]="SHA-256 checksums for every file in this release. Check them with sha256sum -c SHA256SUMS."
)

# --------------------------------------------------------------------------
# Discover the t2-utils package and its apt repo.  The rootfs build writes the
# .deb next to a Packages index (the repo the installed image reads from
# /opt/t2/repo); the exact host directory is an implementation detail, so glob
# for it in one marked place and never guess a path.
# --------------------------------------------------------------------------
shopt -s nullglob
declare -A deb_seen=()
deb_candidates=()
for pat in "$src"/*.deb "$src"/repo/*.deb "$root"/build/rootfs/repo/*.deb; do
    for f in $pat; do
        [ -z "${deb_seen[$f]:-}" ] || continue
        deb_seen[$f]=1
        deb_candidates+=("$f")
    done
done

if [ "${#deb_candidates[@]}" = 0 ]; then
    echo "error: no t2-utils .deb found under $src" >&2
    echo "       looked for *.deb and repo/*.deb; run the full build (step 7) first" >&2
    exit 1
fi
if [ "${#deb_candidates[@]}" -gt 1 ]; then
    echo "error: more than one .deb found, cannot tell which to publish:" >&2
    printf '       %s\n' "${deb_candidates[@]}" >&2
    exit 1
fi

deb=${deb_candidates[0]}
deb_name=$(basename "$deb")
desc[$deb_name]="The t2-utils package for the T2 board: services, scripts and settings. Install it on a running board with apt-get install ./$deb_name."

# The repo directory is where the .deb was written; the in-image repo also
# carries the Packages index, but that is a build stage, not an output.  Ship
# the index with the package when it is alongside or in the kept stage, so a
# user can serve the same repo the board reads from /opt/t2/repo.
repo_dir=$(dirname "$deb")
[ -f "$repo_dir/Packages" ] || repo_dir=$root/build/rootfs/stage/opt/t2/repo
repo_pkg=
if [ -f "$repo_dir/Packages" ]; then
    repo_pkg=t2-utils-apt-repo.tar.gz
    desc[$repo_pkg]="The local apt repository shipped in the image: the t2-utils package and its Packages index."
else
    echo "note: no Packages index found; shipping the .deb on its own" >&2
fi

# --------------------------------------------------------------------------
# Copy.  Fail on a missing required asset, skip a missing optional one.
# --------------------------------------------------------------------------
rm -rf "$dest"
mkdir -p "$dest"

copied=()
for name in "${required[@]}"; do
    if [ ! -f "$src/$name" ]; then
        echo "error: required asset $src/$name is missing" >&2
        echo "       run the full build first: ./docker-build.sh" >&2
        exit 1
    fi
    install -m 644 "$src/$name" "$dest/$name"
    copied+=("$name")
done
for name in "${optional[@]}"; do
    if [ -f "$src/$name" ]; then
        install -m 644 "$src/$name" "$dest/$name"
        copied+=("$name")
    fi
done

install -m 644 "$deb" "$dest/$deb_name"
copied+=("$deb_name")

if [ -n "$repo_pkg" ]; then
    tar -C "$repo_dir" -czf "$dest/$repo_pkg" .
    copied+=("$repo_pkg")
fi

# --------------------------------------------------------------------------
# Checksums (names only, so `sha256sum -c` works in the download directory).
# --------------------------------------------------------------------------
(
    cd "$dest"
    sha256sum "${copied[@]}" > SHA256SUMS
)
copied+=(SHA256SUMS)

# --------------------------------------------------------------------------
# Release-notes file table, in a fixed order: the image users want first, the
# components next, the checksums last.
# --------------------------------------------------------------------------
notes_order=(
    installer.img
    rootfs.ext4.zst
    "$deb_name"
)
[ -n "$repo_pkg" ] && notes_order+=("$repo_pkg")
notes_order+=(
    Image
    rk3568-t2.dtb
    t2-mainline-boot.img
    u-boot.itb
    idbloader.img
    u-boot-installer.itb
    idbloader-installer.img
    u-boot-initial-env
    u-boot-installer-initial-env
    Image.old
    SHA256SUMS
)

if [ -n "$notes" ]; then
    mkdir -p "$(dirname "$notes")"
    {
        echo "## Files in this release"
        echo
        echo "| File | What it is |"
        echo "|---|---|"
        for name in "${notes_order[@]}"; do
            [ -f "$dest/$name" ] || continue
            printf '| `%s` | %s |\n' "$name" "${desc[$name]}"
        done
    } > "$notes"
fi

echo "collect-release-artifacts: ${#copied[@]} asset(s) in $dest"
for name in "${copied[@]}"; do
    printf '  %s\n' "$name"
done
[ -n "$notes" ] && echo "  notes section: $notes"
