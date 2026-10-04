#!/usr/bin/env bash
#
# Collect the release assets from a finished build into one directory, and
# write the file list and the pinned-component table for the release notes.
#
# Usage: tools/collect-release-artifacts.sh [options] [SRC] [DEST]
#
#   SRC             build output directory       (default: build/out)
#   DEST            staging directory for assets (default: dist/release)
#   --src DIR       same as the first positional argument
#   --dest DIR      same as the second positional argument
#   --lock FILE     components.lock to ship and read (default: <repo>/components.lock)
#   --notes FILE    also write the "files" section of the release notes
#   -h, --help
#
# SRC is build/out after a full build (build-all.sh).  The release carries only
# what this repository builds: installer.img, rootfs.ext4.zst, the t2-initramfs
# package and components.lock.  The kernel, U-Boot, t2-utils and the raw
# boot-chain files are built by the three component repositories and pinned in
# components.lock; they are not this release's to publish.
#
# build/out holds exactly one .deb, the t2-initramfs package.  It is found by
# glob, never by a hard-coded version (the version lives in the artefact name,
# which is the profile's business), and the script fails if it finds zero, more
# than one, or a package that is not t2-initramfs.
#
# The release carries a SHA256SUMS file it generates itself; run
# `sha256sum -c SHA256SUMS` in the download directory to check it.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)

src=$root/build/out
dest=$root/dist/release
notes=
lock=$root/components.lock

usage() {
    cat <<'EOF'
Collect the release assets from a finished build.

Usage: tools/collect-release-artifacts.sh [options] [SRC] [DEST]
  SRC           build output directory        (default: build/out)
  DEST          staging directory for assets  (default: dist/release)
  --src DIR     same as the first positional argument
  --dest DIR    same as the second positional argument
  --lock FILE   components.lock to ship and read (default: <repo>/components.lock)
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
        --lock) lock=$2; shift 2 ;;
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
[ -f "$lock" ] || {
    echo "error: no components.lock at $lock" >&2
    echo "       the release must name the component versions it was built from" >&2
    exit 1
}

# --------------------------------------------------------------------------
# The release assets.  One source of truth for what is copied and for the
# release-notes table, so the two can never drift apart.
# --------------------------------------------------------------------------
# Every release carries these; the script fails without them.
required=(
    installer.img
    rootfs.ext4.zst
)

declare -A desc=(
    [installer.img]="Write this SD-card installer image to a card and boot the board. It installs the system to the eMMC. This is the file most users want."
    [rootfs.ext4.zst]="The Ubuntu root filesystem, zstd-compressed. The installer writes it to the eMMC."
    [components.lock]="The exact component releases and artefact sha256s this image was built from: the kernel, U-Boot and t2-utils that went into it. Commit this if you rebuild the image."
    [SHA256SUMS]="SHA-256 checksums for every file in this release. Check them with sha256sum -c SHA256SUMS."
)

# --------------------------------------------------------------------------
# Discover the t2-initramfs package.  build/out holds exactly one .deb (the
# boot initramfs this repository builds); more than one would mean two builds'
# output were mixed, and a different package name would be an unexpected input.
# --------------------------------------------------------------------------
shopt -s nullglob
deb_candidates=("$src"/*.deb)
if [ "${#deb_candidates[@]}" = 0 ]; then
    echo "error: no t2-initramfs .deb found under $src" >&2
    echo "       the rootfs build (step 4) writes it; run the full build first" >&2
    exit 1
fi
if [ "${#deb_candidates[@]}" -gt 1 ]; then
    echo "error: more than one .deb found, build/out must hold only t2-initramfs:" >&2
    printf '       %s\n' "${deb_candidates[@]}" >&2
    exit 1
fi

deb=${deb_candidates[0]}
deb_name=$(basename "$deb")
case "$deb_name" in
    t2-initramfs_*_all.deb) ;;
    *)
        echo "error: expected the t2-initramfs package, found $deb_name" >&2
        exit 1
        ;;
esac
desc[$deb_name]="The boot initramfs the board boots, and the payload an on-board kernel upgrade needs: linux-image-*-t2 depends on this package and assembles a bootable FIT from its /boot/initramfs-t2.gz ramdisk. Install with apt-get install ./$deb_name."

# --------------------------------------------------------------------------
# Copy.  Fail on a missing required asset.
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

install -m 644 "$deb" "$dest/$deb_name"
copied+=("$deb_name")

install -m 644 "$lock" "$dest/components.lock"
copied+=(components.lock)

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
# package they may reinstall next, the lock, the checksums last.
# --------------------------------------------------------------------------
notes_order=(
    installer.img
    rootfs.ext4.zst
    "$deb_name"
    components.lock
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
        echo
        echo "## Components pinned in this release"
        echo
        echo "\`components.lock\` pins each component artefact by sha256. These are the component releases this image was built from:"
        echo
        echo "| Component | Repository | Release | Commit |"
        echo "|---|---|---|---|"
        python3 - "$lock" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    lock = json.load(fh)
for name in sorted(lock.get("components", {})):
    spec = lock["components"][name]
    commit = (spec.get("commit") or "")[:12] or "-"
    print(f"| {name} | {spec.get('repo', '?')} | {spec.get('tag', '?')} | `{commit}` |")
PY
    } > "$notes"
fi

echo "collect-release-artifacts: ${#copied[@]} asset(s) in $dest"
for name in "${copied[@]}"; do
    printf '  %s\n' "$name"
done
[ -n "$notes" ] && echo "  notes section: $notes"
