# Host environment for the ZSpace T2 build (docs/building.md).
#
# This image only supplies the host tools.  Everything is fetched and built by
# the repository's own scripts, run from a bind mount at /work by
# docker-build.sh - nothing from the tree is copied into the image, and
# build/out/ is written to the host.
#
# Details that matter:
#   * The target is always arm64; the image is built and run on the host's own
#     architecture, amd64 or arm64.  Ubuntu 24.04 is what the package list was
#     written against.
#   * gcc-aarch64-linux-gnu is per-architecture.  On amd64 it is the cross gcc
#     (gcc 13) and keeps the target headers and libraries under
#     /usr/aarch64-linux-gnu.  On arm64 it is a meta package for the native
#     gcc, which keeps them in /usr/include and /usr/lib/aarch64-linux-gnu;
#     the /usr/aarch64-linux-gnu flags in rootfs/initramfs/build.sh are then
#     simply ignored (the directory does not exist) and the native defaults
#     apply.
#   * The target header packages are named explicitly, selected below from
#     dpkg --print-architecture: libc6-dev-arm64-cross and
#     linux-libc-dev-arm64-cross on amd64, libc6-dev (the native headers) on
#     arm64.  The cross names are only Recommends of gcc-aarch64-linux-gnu, and
#     --no-install-recommends would drop them.  Without the target headers the
#     compiler falls back to the wrong /usr/include and BusyBox fails on
#     bits/libc-header-start.h.
#   * proot is arch-specific.  On amd64 rootfs/t2-distro.py downloads the
#     pinned upstream static proot; on arm64 the chroot is native and it uses
#     the host's proot, which is why the arm64 branch installs proot here (the
#     Ubuntu .deb cannot be run out of a dpkg-deb -x cache: it needs
#     libtalloc2).
#   * The apt lists stay in the image on purpose: on amd64
#     rootfs/t2-distro.py fetches qemu-user-static with `apt-get download` and
#     unpacks it with dpkg-deb, which needs no root and no binfmt_misc.  It
#     resolves its version from these lists, so rebuild the image when they go
#     stale.
#   * bzip2 is not in the docs table but the pinned BusyBox source is a
#     .tar.bz2.  cpio packs the initramfs into the boot FIT's ramdisk: the
#     kernel Image no longer embeds the tree, and the kernel's in-tree
#     gen_init_cpio is not reachable from this repository.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    CROSS_COMPILE=aarch64-linux-gnu-

RUN apt-get update \
    && arch=$(dpkg --print-architecture) \
    && case "$arch" in \
         amd64) target_pkgs="libc6-dev-arm64-cross linux-libc-dev-arm64-cross" ;; \
         arm64) target_pkgs="libc6-dev proot" ;; \
         *) echo "unsupported host architecture: $arch" >&2; exit 1 ;; \
       esac \
    && apt-get install -y --no-install-recommends \
        build-essential make git ca-certificates \
        gcc-aarch64-linux-gnu $target_pkgs \
        bc bison flex libssl-dev device-tree-compiler \
        python3 python3-dev python3-setuptools python3-pyelftools swig \
        fakeroot e2fsprogs zstd dosfstools mtools util-linux kmod cpio \
        curl tar bzip2 \
    && apt-get clean

WORKDIR /work

CMD ["./build-all.sh"]
