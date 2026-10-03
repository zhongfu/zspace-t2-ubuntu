# Building the images

This page sets up the host, explains the build order and the entry points, and
notes the firmware that is not in the repository.  All scripts resolve their
paths from their own location, so the repository works from any clone path.

## Host setup

Build on a Linux x86-64 host with about 40 GB free.  Install these packages
(Ubuntu/Debian names):

| Package | Needed by |
|---|---|
| `build-essential`, `make`, `git` | all builds; `git` also fetches the trees |
| `gcc-aarch64-linux-gnu` | cross compiler (`aarch64-linux-gnu-gcc`) for kernel and U-Boot |
| `bc`, `bison`, `flex`, `libssl-dev` | kernel build |
| `device-tree-compiler` | device tree compile (`dtc`) |
| `python3`, `swig` | U-Boot `binman` and its `pylibfdt` module |
| `python3-dev`, `python3-setuptools`, `python3-pyelftools` | U-Boot host tools |
| `fakeroot`, `e2fsprogs` | build the rootfs ext4 image without root (`mke2fs`, `e2fsck`, `debugfs`) |
| `zstd` | compressed kernel modules and the installer payload |
| `dosfstools`, `mtools` | the FAT boot tree on the installer card (`mkfs.vfat`, `mcopy`) |
| `curl`, `tar` | fetch and unpack the Ubuntu base tarball |
| `qemu-user-static`, `proot` | run the arm64 chroot for package installation |
| `dpkg-deb`, `apt-get` | extract `qemu-user-static` and install packages in the chroot |

Notes:

* The rootfs build fetches `qemu-user-static` and a pinned `proot` itself, with
  no host root.  You still need a Debian/Ubuntu host, `apt-get`, and network
  access to the Ubuntu archive and to `proot.gitlab.io`.
* `CROSS_COMPILE` defaults to `aarch64-linux-gnu-`.  Set it if your toolchain
  has another prefix.  A custom toolchain may also need `LD_LIBRARY_PATH`.
* `libncurses-dev` is only needed if you want `make menuconfig`.

## Build order

Run the entry points from the repository root, in this order.  Each script has
`-h`.

| Step | Command | Output |
|---|---|---|
| 1 | `rootfs/fetch.sh` | `rootfs/firmware/` (vendor WiFi/BT blobs) |
| 2 | `rootfs/initramfs/build.sh` | `build/initramfs` (static busybox initramfs tree) |
| 3 | `kernel/fetch.sh` | `build/linux` (mainline tag `v7.3-rc5`) |
| 4 | `kernel/build.sh` | `build/out/Image`, `build/out/rk3568-t2.dtb`, `build/out/modules/` |
| 5 | `u-boot/fetch.sh` | `build/uboot` (`v2026.07`), `build/rkbin` (BL31 and DDR blobs) |
| 6 | `u-boot/build.sh` | `build/out/u-boot.itb`, `idbloader.img`, `u-boot-installer.itb`, `idbloader-installer.img`, `u-boot-initial-env`, `u-boot-installer-initial-env` |
| 7 | `rootfs/build.sh` | `build/out/rootfs.ext4` |
| 8 | `images/build-installer.sh` | `build/out/installer.img` |

```sh
rootfs/fetch.sh                          # 1
rootfs/initramfs/build.sh                # 2
kernel/fetch.sh  && kernel/build.sh      # 3, 4
u-boot/fetch.sh  && u-boot/build.sh      # 5, 6
rootfs/build.sh                          # 7
images/build-installer.sh                # 8
```

**Step 2 must precede step 4.**  The kernel build rewrites
`CONFIG_INITRAMFS_SOURCE` to `<repo>/build/initramfs`, the tree that
`rootfs/initramfs/build.sh` lays out.  If you build the kernel first, the
embedded initramfs is wrong.

`rootfs/fetch.sh` runs first because `rootfs/build.sh` stops without the vendor
firmware.  `rootfs/initramfs/build.sh` builds the static busybox and the
initramfs layout.  `kernel/build.sh` applies the five patches in
`kernel/patches/`, copies `kernel/config/kernel.config`, runs `olddefconfig`,
and builds `Image`, `dtbs`, and `modules`.  `u-boot/build.sh` builds two images
from one board control: the plain image for the eMMC and the installer image for
the card, each with its own `idbloader.img`.  `rootfs/build.sh` builds the
rootfs ext4 image; `images/build-installer.sh` assembles the SD card image.

All build output goes to `build/`, which git ignores.  Finished artefacts go to
`build/out/`.

Timings, as measured during bring-up: a full kernel build took **6m46s
wall** (98 minutes CPU) at `-j20` on the reference host.  A later run with the
kernel already built finished in about **30 s**.  A U-Boot build and a rootfs
build are faster than the kernel, but both include downloads, so treat them as
network-bound.  No separate wall-clock was recorded for U-Boot or rootfs.

## Firmware

The Broadcom WiFi and Bluetooth firmware for the AP6275P module comes from the
vendor rootfs and is **not redistributable**.  It is not in this repository and
cannot be shipped.  `rootfs/fetch.sh` collects it from a T2 that still runs the
vendor firmware, or from a vendor update package.  `rootfs/build.sh` stops with
a clear message when the firmware is missing.

The mainline driver expects these names in `/lib/firmware/brcm/`:

| Mainline name | Vendor blob |
|---|---|
| `brcmfmac43752-pcie.bin` | `fw_bcm43752a2_pcie_ag.bin` |
| `brcmfmac43752-pcie.txt` | `nvram_AP6275P.txt` |
| `brcmfmac43752-pcie.clm_blob` | `clm_bcm43752a2_ag.blob` |

Only the `.bin` is mandatory.  The module can enumerate before the root
filesystem is switched in, so the same files must also be in the **initramfs**,
or the load fails with `-ENOENT`.

The RTL8156B Ethernet firmware (`rtl_nic/rtl8156b-2.fw`) and the wireless
regulatory database are redistributable, so the build fetches those normally.

## Docker

**Not implemented yet.**  The repository builds on the host as described above;
there is no `Dockerfile`.

A container image would need: a Debian/Ubuntu base; the packages in the table
above; the cross toolchain; the source trees and the firmware staged in; and
network access for the fetches.  Real caveats:

* **Privilege.**  The kernel and U-Boot builds need no root, and the rootfs
  build avoids host root with `fakeroot` and `proot`.  Anything that mounts a
  loop device, or uses the `sudo` chroot backend, needs extra privileges.
* **Kernel build time.**  The kernel build is CPU-heavy for several minutes.
  Run it once, cache the tree, and avoid many parallel container builds on one
  host.
* **Downloads.**  Fetching mainline Linux, U-Boot, rkbin, the Ubuntu base
  tarball, and the chroot tools needs network and a cache.  Bake a warm cache
  into the image or mount one as a volume.
* **Devices.**  The serial console and maskrom work need USB access:
  `--device=/dev/ttyUSB0` for the adapter, and permission for raw USB access so
  `rkdeveloptool` can see `2207:350a`.  `tools/t2-flash.py` resolves
  `rkdeveloptool` from `PATH` or `T2_RKDEVELOPTOOL`.
* **Host side of the gadget link.**  `tools/t2-gadget-link.py` drives
  NetworkManager on the host; a container would need host networking and D-Bus.
