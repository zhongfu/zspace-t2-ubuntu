# Host environment for the ZSpace T2 build (docs/building.md).
#
# This image only supplies the host tools.  Everything is fetched and built by
# the repository's own scripts, run from a bind mount at /work by
# docker-build.sh - nothing from the tree is copied into the image, and
# build/out/ is written to the host.
#
# Details that matter:
#   * Ubuntu 24.04 is what the package list was written against.  Its
#     gcc-aarch64-linux-gnu is gcc 13 and installs headers and libraries under
#     /usr/aarch64-linux-gnu, which rootfs/initramfs/build.sh expects.
#   * The apt lists stay in the image on purpose: rootfs/t2-distro.py fetches
#     qemu-user-static with `apt-get download` and unpacks it with dpkg-deb,
#     which needs no root and no binfmt_misc.  It resolves the version from
#     these lists, so rebuild the image when they go stale.
#   * bzip2 is not in the docs table but the pinned BusyBox source is a
#     .tar.bz2.  cpio is not needed: the kernel embeds the initramfs tree with
#     its in-tree gen_init_cpio.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    CROSS_COMPILE=aarch64-linux-gnu-

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential make git ca-certificates \
        gcc-aarch64-linux-gnu \
        bc bison flex libssl-dev device-tree-compiler \
        python3 python3-dev python3-setuptools python3-pyelftools swig \
        fakeroot e2fsprogs zstd dosfstools mtools util-linux \
        curl tar bzip2 \
    && apt-get clean

WORKDIR /work

CMD ["./build-all.sh"]
