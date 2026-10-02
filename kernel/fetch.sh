#!/usr/bin/env bash
#
# Fetch the mainline Linux tree for the ZSpace T2 build.
#
# Clones tag v7.3-rc5 (the base the five kernel/patches/*.patch apply to) into
# <repo>/build/kernel. Shallow is enough: the patches are applied on top as
# commits, nothing older is needed.
#
# Safe to re-run: if build/kernel already exists it refuses and says so instead
# of clobbering a tree - possibly one you have built in.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)

url=${LINUX_URL:-https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git}
tag=v7.3-rc5

build=$repo/build
tree=$build/kernel

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help]

Clone mainline Linux $tag into $tree.

Environment:
  LINUX_URL   git URL to clone from (default: kernel.org torvalds/linux)
EOF
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

if [ -e "$tree" ]; then
    echo "error: $tree already exists." >&2
    echo "       Remove it first if you want a fresh $tag checkout." >&2
    exit 1
fi

mkdir -p "$build"

echo "== fetching Linux $tag =="
echo "  from : $url"
echo "  into : $tree"

if ! git clone --branch "$tag" --depth 1 --quiet "$url" "$tree"; then
    echo "error: git clone failed." >&2
    echo "       Check the network and $url, or set LINUX_URL." >&2
    exit 1
fi

echo "  done: $(git -C "$tree" describe --tags 2>/dev/null || echo "$tag")"
