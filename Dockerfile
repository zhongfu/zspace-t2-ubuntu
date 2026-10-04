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

ARG PROOT_COMMIT=25dc6a3134891f98a79f57ce1c2c1b23ff15cad1

RUN apt-get update \
    && arch=$(dpkg --print-architecture) \
    && case "$arch" in \
         amd64) target_pkgs="libc6-dev-arm64-cross linux-libc-dev-arm64-cross" ;; \
         arm64) target_pkgs="libc6-dev libtalloc-dev libseccomp-dev" ;; \
         *) echo "unsupported host architecture: $arch" >&2; exit 1 ;; \
       esac \
    && apt-get install -y --no-install-recommends \
        build-essential make git ca-certificates \
        gcc-aarch64-linux-gnu $target_pkgs \
        bc bison flex libssl-dev device-tree-compiler \
        python3 python3-dev python3-setuptools python3-pyelftools swig \
        fakeroot e2fsprogs zstd dosfstools mtools util-linux kmod cpio \
        curl tar bzip2 \
    && if [ "$arch" = arm64 ]; then \
         # arm64 runs the rootfs chroot with the host's proot, and nothing older
         # than upstream v5.5.0 works on this runner: Ubuntu 24.04's 5.1.0 and
         # the archive's 5.4.0-3 crash the guest, and a v5.4.1 built from source
         # fails as well - all in proot's own re-exec loader, which native arm64
         # needs and amd64's qemu path never uses (measured 2026-10-04 on
         # ubuntu-26.04-arm, kernel 7.0.0-1012-azure; v5.5.0 runs the probe
         # cleanly).  No aarch64 static binary is published, so build it from the
         # pinned commit.  amd64 downloads the published static binary
         # (rootfs/t2-distro.py), which is that same commit.
         git clone -q --depth 1 --branch v5.5.0 https://github.com/proot-me/proot /tmp/proot \
         && [ "$(git -C /tmp/proot rev-parse HEAD)" = "$PROOT_COMMIT" ] \
         # v5.5.0's arm64 syscall table has no [439] = PR_faccessat2 entry, though
         # 439 is that syscall's number on arm64 as well and x86_64's table has
         # it.  proot therefore does not path-translate faccessat2 on arm64, and
         # a guest access(2) - dash's test -w, which is what ucf runs inside
         # openssh-server's postinst - is answered for the *host* path, so it
         # fails with ENOENT and that postinst dies.  Patch the entry in; the
         # grep keeps this honest if the pinned commit ever changes.
         && sed -i '/\[452\] = PR_fchmodat2,/i\    [439] = PR_faccessat2,' /tmp/proot/src/syscall/sysnums-arm64.h \
         && grep -q '\[439\] = PR_faccessat2,' /tmp/proot/src/syscall/sysnums-arm64.h \
         && make -s -C /tmp/proot/src -j"$(nproc)" \
         && install -m 755 /tmp/proot/src/proot /usr/local/bin/proot \
         && rm -rf /tmp/proot; \
       fi \
    && apt-get clean

WORKDIR /work

CMD ["./build-all.sh"]
