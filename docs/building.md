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
| `dpkg-deb`, `apt-get` | build the `t2-utils` package, extract `qemu-user-static`, install packages in the chroot |

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
| 2 | `rootfs/initramfs/build.sh` | `build/initramfs` (static busybox + zstd initramfs tree) |
| 3 | `kernel/fetch.sh` | `build/kernel` (mainline tag `v7.3-rc5`) |
| 4 | `kernel/build.sh` | `build/out/Image`, `build/out/rk3568-t2.dtb`, `build/out/modules/` |
| 5 | `u-boot/fetch.sh` | `build/uboot` (`v2026.07`), `build/rkbin` (BL31 and DDR blobs) |
| 6 | `u-boot/build.sh` | `build/out/u-boot.itb`, `idbloader.img`, `u-boot-installer.itb`, `idbloader-installer.img`, `u-boot-initial-env`, `u-boot-installer-initial-env` |
| 7 | `rootfs/build.sh` | `build/out/rootfs.ext4` (with `t2-utils` installed from the in-image `/opt/t2/repo`) |
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
initramfs layout, plus a static `zstd` built from a pinned tarball: the
installer streams its payloads through `zstd -dc` on the board, and busybox has
no zstd applet, so both live in the initramfs.  `kernel/build.sh` applies the
five patches in `kernel/patches/`, copies `kernel/config/kernel.config`, runs
`olddefconfig`, and builds `Image`, `dtbs`, and `modules`; a tree that already
carries all five patches is rebuilt as it is, so a re-run does not apply them
twice.
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

## Rebuilding one step

`build-all.sh` can run a subset of the eight steps:

```sh
./build-all.sh --only 6,8     # U-Boot and the installer image only
./build-all.sh --from 7       # rootfs and installer image
./build-all.sh --to 4         # firmware, initramfs, kernel tree, kernel
./build-all.sh --skip 4       # everything but the kernel build
./build-all.sh --list         # the steps and their commands
```

Steps 3 and 5 are skipped when their output tree is already there (they fetch
and refuse to clobber a tree).  Steps 2, 4, 6, 7 and 8 record a sha256 of their
inputs under `build/.stamps/`, so a re-run whose inputs are unchanged prints
`[skip] 7/8 rootfs: unchanged (build/.stamps/step7-rootfs.sha256)` and rebuilds
nothing.  The stamps hash file *content*, so editing a file changes a stamp but
a bare `touch` does not.  To force one step, delete its stamp (or edit one of
its inputs):

| Step | Stamp | Inputs |
|---|---|---|
| 2 | `step2-initramfs.sha256` | the `rootfs/initramfs/` tree (its output is embedded in the kernel Image, so a changed `init` or `installer.sh` must invalidate step 4 too) |
| 4 | `step4-kernel.sha256` | `kernel/` scripts, config and patches, `build/initramfs`, the kernel tree's HEAD |
| 6 | `step6-uboot.sha256` | `u-boot/` scripts, configs, DTS and patches, the rkbin blobs, the U-Boot tree's HEAD |
| 7 | `step7-rootfs.sha256` | the `rootfs/profiles/t2-base` tree, the `rootfs/packages/` source, `rootfs/firmware/`, `rootfs/initramfs/firmware/`, the rootfs scripts and `lib/`, `build/out/Image`, `build/out/rk3568-t2.dtb`, `build/out/modules` |
| 8 | `step8-installer.sha256` | `images/` tools and the config template, `Image`, `rk3568-t2.dtb`, `u-boot.itb`, `idbloader.img`, `rootfs.ext4.zst` (plus `Image.old`/`u-boot-initial-env` when present) |

Step 8 consumes the U-Boot artefacts and step 7 does not, so a change to a
step-6 input (for example `u-boot/dts/rk3568-t2.dts`) re-runs steps 6 and 8 but
leaves 7 alone.

### Rootfs stage reuse

Step 7 (`rootfs/build.sh` -> `t2-distro.py`) keeps its working tree in
`build/rootfs/stage` and no longer wipes it on every run.  Each stage that
modifies the tree records a stamp in `build/rootfs/stage/.t2-stamps/`, and a
re-run only rebuilds the stages whose inputs changed:

| Stage | Stamp | Stale when |
|---|---|---|
| base | the pinned base tarball sha256 | the tarball in `profile/base.json` changes |
| packages | the sha256 of `packages.txt` | a package is added, removed or renamed |
| overlay | the hash of the profile `overlay/` tree | an overlay file is edited, added or removed |
| debs | the `rootfs/packages/t2-utils` tree hash and the package version | a payload file, `control.in`, `conffiles`, `postinst` or `build.sh` changes, or the profile's base release changes |
| hooks | the hash of the hook scripts, the hook environment and the firmware tree | a hook is edited, or the firmware changes |
| modules | the kernel tree's `.config` sha256, its release, and the hash of its built `.ko` | the kernel config or release changes, or a source/patches edit rebuilds the modules (a patch edit keeps the config and release identical, so the `.ko` hash is what catches it) |

A rebuilt stage cascades into the stages that read its result: a packages
rebuild re-runs overlay, debs and hooks; an overlay rebuild re-runs debs and
hooks; a debs rebuild re-runs hooks; a changed base tarball wipes the whole
stage, so every stage re-runs (including the 35 package apt install).  The
stamps are lifted out around `mke2fs`, so they never land in the image.

To force a full rootfs rebuild, delete the stage tree (the stamps go with it):

```sh
rm -rf build/rootfs/stage        # next rootfs build re-extracts and re-apts
rm -f build/.stamps/step7-rootfs.sha256   # make build-all re-run step 7
```

A rootfs build that adopts an existing stage starts at the first stale stage and
still ends with `build/out/rootfs.ext4` + `.zst`, and the file-based checks
(`verify`) still run and must pass.

Measured 2026-10-03: with every stage stamp matching, `rootfs/build.sh` rebuilt
the image in **13.6 s wall** (t2-distro 10.9 s) and all 39 checks passed.  A run
that had to rebuild overlay + hooks + modules (base and packages adopted from
the previous build's manifest) took **106 s wall**.  A full `build-all.sh` whose
inputs were all unchanged - steps 4, 6, 7 and 8 all stamped - finished in
**1.4 s wall**, against the reference full run's 1698 s (`manifest.json`).

## The board userspace package (`t2-utils`)

The board support files (systemd units, `/etc` drop-ins, `/usr/local/sbin`
helpers) are a Debian package, `t2-utils`, not files copied from the profile
overlay.  Step 7 builds it from `rootfs/packages/t2-utils/` with `dpkg-deb`
(no debhelper: the build image has dpkg but no dpkg-dev), drops the `.deb` into
a flat apt repository inside the image at `/opt/t2/repo`, writes
`/etc/apt/sources.list.d/t2.list` (`deb [trusted=yes] file:/opt/t2/repo ./`)
and installs it *from that repo* in the chroot.  The `/etc` files are
conffiles, and the `postinst` re-enables the units idempotently without ever
failing the dpkg run.

A running board can therefore reinstall or upgrade the package offline:

```sh
apt-get update && apt-cache policy t2-utils
apt-get install --reinstall -y t2-utils      # or: apt-get upgrade
```

A newer build applies without reflashing: copy its `.deb` to the board and
`apt-get install -y /path/t2-utils_<version>_all.deb` (apt resolves the
`Depends` against what is already installed).  `rootfs/README.md` has the
details.

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
