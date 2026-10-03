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
| `kmod` | `depmod`, which `make modules_install` runs for the rootfs image (`modules.dep`) |
| `zstd` | compressed kernel modules and the installer payload |
| `dosfstools`, `mtools` | the FAT boot tree on the installer card (`mkfs.vfat`, `mcopy`) |
| `curl`, `tar` | fetch and unpack the Ubuntu base tarball |
| `bzip2` | unpack the pinned BusyBox source (`.tar.bz2`) |
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
| 3 | `kernel/fetch.sh` | `build/kernel` (mainline tag `v7.3-rc5`) |
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

`./build-all.sh` runs the same eight steps in this order, skipping a fetch whose
tree is already there, so it is safe to re-run; `./docker-build.sh` runs it in
the container below.  Step 1 is the one thing they cannot do for you: the vendor
firmware needs a source, so `build-all.sh` stops with the `rootfs/fetch.sh`
invocation to run when it is missing.

**Step 2 must precede step 4.**  The kernel build rewrites
`CONFIG_INITRAMFS_SOURCE` to `<repo>/build/initramfs`, the tree that
`rootfs/initramfs/build.sh` lays out.  If you build the kernel first, the
embedded initramfs is wrong.

`rootfs/fetch.sh` runs first because `rootfs/build.sh` stops without the vendor
firmware.  `rootfs/initramfs/build.sh` builds the static busybox and the
initramfs layout.  `kernel/build.sh` applies the five patches in
`kernel/patches/`, copies `kernel/config/kernel.config`, runs `olddefconfig`,
and builds `Image`, `dtbs`, and `modules`; a tree that already carries all five
patches is rebuilt as it is, so a re-run does not apply them twice.
`u-boot/build.sh` builds two images from one board control: the plain image for
the eMMC and the installer image for the card, each with its own
`idbloader.img`.  `rootfs/build.sh` builds the rootfs ext4 image;
`images/build-installer.sh` assembles the SD card image.

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

`Dockerfile` pins the host tools above, and `docker-build.sh` runs the whole
build inside it - the same eight steps, through `build-all.sh`:

```sh
./docker-build.sh                    # the whole build -> build/out/installer.img
./docker-build.sh kernel/build.sh    # one step, in the same environment
./docker-build.sh bash               # a shell in the same environment
```

The image carries no repository content: the tree is bind-mounted at `/work`,
the container runs with your uid and gid, so `build/` stays yours, and the only
things baked in are Ubuntu 24.04 and the packages listed above (plus `bzip2` for
the BusyBox source).  The apt lists are left in the image on purpose -
`rootfs/t2-distro.py` fetches `qemu-user-static` with `apt-get download`.  Build
the image once; rebuild it (`docker build --no-cache`) when those lists go
stale, because the download resolves its version from them.

Caveats that remain:

* **Network.**  The fetch steps still clone Linux, U-Boot and rkbin, unpacks the
  Ubuntu base tarball, downloads the BusyBox source, collects `qemu-user-static`
  and `proot`, and apt-installs the profile inside the chroot.  All of it is
  cached under `build/`, so a second run only re-downloads inside the chroot.
* **`ptrace`.**  `docker-build.sh` passes `--security-opt seccomp=unconfined`:
  the default rootfs backend is `proot`, which needs `ptrace`, and Docker's
  default seccomp profile denies it.
* **Privilege.**  Unchanged from a host build: no loop devices and no host root;
  the image stage is `fakeroot` + `mke2fs -d`, and `qemu-user-static` is
  unpacked rather than bound into `binfmt_misc`.
* **Kernel build time.**  Unchanged: minutes, CPU-bound.  `JOBS=N
  ./docker-build.sh` limits it.
* **Devices.**  The container is only the build.  The serial console, maskrom
  and the gadget link in `tools/` stay on the host and need USB access there.
