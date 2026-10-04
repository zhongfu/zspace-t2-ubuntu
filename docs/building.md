# Building the images

This page sets up the host, explains the build order and the entry points, and
notes the firmware the images embed.  All scripts resolve their
paths from their own location, so the repository works from any clone path.

## The four repositories

This repository owns the initramfs, the installer, the rootfs and the image
assembly.  The kernel, U-Boot and the board userspace (`t2-utils`) come from
`zspace-t2-kernel`, `zspace-t2-bootloader` and `zspace-t2-ubuntu-utils` as
released artefacts.  `components.lock` pins every one of those artefacts by
sha256, and build step 1 fetches and verifies them; nothing in this tree builds
a kernel or U-Boot, and the component sources never appear here.

## Host setup

Build on a Linux x86-64 or arm64 host with about 40 GB free.  The target is
always arm64; the host architecture only decides how the rootfs chroot runs.
On x86-64 the arm64 guest binaries are emulated: the build fetches
`qemu-user-static` and a pinned upstream `proot`.  On arm64 they run natively:
no qemu is fetched, and the host's own `proot` is used.  Install these packages
(Ubuntu/Debian names):

| Package | Needed by |
|---|---|
| `build-essential`, `make`, `git` | all builds |
| `gcc-aarch64-linux-gnu` | cross compiler for the initramfs BusyBox and `t2-keywait` |
| `fakeroot`, `e2fsprogs` | build the rootfs ext4 image without root (`mke2fs`, `e2fsck`, `debugfs`) |
| `cpio` | pack the initramfs into the boot FIT's ramdisk subimage |
| `zstd` | compress the rootfs image and the installer payload |
| `dosfstools`, `mtools` | the FAT boot tree on the installer card (`mkfs.vfat`, `mcopy`) |
| `curl`, `tar` | fetch and unpack the Ubuntu base tarball |
| `bzip2` | unpack the pinned BusyBox source (`.tar.bz2`) |
| `qemu-user-static`, `proot` | run the arm64 chroot for package installation (on x86-64 hosts the rootfs build fetches both; on arm64 hosts `proot` must be installed, and the Dockerfile does) |
| `dpkg-deb`, `apt-get` | build the `t2-initramfs` package, unpack the fetched component debs, install packages in the chroot |

The kernel and U-Boot toolchains (`bc`, `bison`, `flex`, `libssl-dev`,
`device-tree-compiler`, `swig`, the `python3` U-Boot modules) belong to
`zspace-t2-kernel` and `zspace-t2-bootloader`; this repository neither builds
nor needs them.  `libncurses-dev` is only needed to edit a kernel config, also
there.

Notes:

* The rootfs build fetches its chroot tools itself on x86-64 hosts, with no
  host root: `qemu-user-static` and a pinned upstream `proot`
  (`proot.gitlab.io`), so you need network access to that site.  On arm64
  hosts the guest binaries are native, so no qemu is needed and the build uses
  the host's own `proot` (the pinned upstream build is x86-64 only, and the
  Ubuntu package cannot run out of the build's unpacked cache).  Install
  `proot` there; the Dockerfile installs it in the image.  Either way you need
  a Debian/Ubuntu host, `apt-get`, and network access to the Ubuntu archive.
* `CROSS_COMPILE` defaults to `aarch64-linux-gnu-`.  Set it if your toolchain
  has another prefix.  A custom toolchain may also need `LD_LIBRARY_PATH`.

## Build order

Run the entry points from the repository root, in this order.  Each script has
`-h`.

| Step | Command | Output |
|---|---|---|
| 1 | `./build-all.sh --only 1` | `build/components/` (the pinned kernel, U-Boot and t2-utils artefacts), staged into `build/out/` |
| 2 | `rootfs/fetch.sh` | `rootfs/firmware/brcm/` (verified blobs and mainline names) |
| 3 | `rootfs/initramfs/build.sh` | `build/initramfs` (static busybox + zstd initramfs tree) and `build/initramfs.gz` |
| 4 | `rootfs/build.sh` | `build/out/rootfs.ext4`, `.zst`, and the `t2-initramfs` package |
| 5 | `images/build-installer.sh` | `build/out/installer.img` |

```sh
./build-all.sh --only 1                  # 1
rootfs/fetch.sh                          # 2
rootfs/initramfs/build.sh                # 3
rootfs/build.sh                          # 4
images/build-installer.sh                # 5
```

`./build-all.sh` runs the same five steps in this order, skipping a step whose
outputs are unchanged, so it is safe to re-run; `./docker-build.sh` runs it in
the container below.  `./build-all.sh` can run a subset with `--only`, `--from`,
`--to` and `--skip`.

Step 1 reads `components.lock`: for each pinned artefact it takes the bytes from
a local directory when `T2_COMPONENTS_DIR` is set (offline and development
builds), otherwise from the component's GitHub release.  The lock records the
org those releases live under; `T2_COMPONENTS_ORG` and `T2_COMPONENTS_URL_BASE`
override it for a fork or a mirror.  Every artefact is hashed before use.  A
mismatch stops the build, so an image never mixes component versions.  The raw
kernel and boot-chain files are staged into `build/out/`, where the later steps
read every input.  Step 2 verifies the vendor blobs committed in
`rootfs/firmware/brcm/` and writes the mainline names there; it needs no
network.

Step 3 (`rootfs/initramfs/build.sh`) builds the static busybox and the
initramfs layout, plus a static `zstd` built from a pinned tarball: the
installer streams its payloads through `zstd -dc` on the board, and busybox has
no zstd applet, so both live in the initramfs.  It then packs the tree into
`build/initramfs.gz`.  That file is the boot FIT's **ramdisk** and the payload
of the `t2-initramfs` package, which is why nothing else has to run before it.

Step 4 (`rootfs/build.sh`, which runs `t2-distro.py`) builds the rootfs ext4
image.  It installs the fetched `t2-utils` package and the fetched
`linux-modules-<rel>-t2` package inside the chroot, builds the `t2-initramfs`
package here, and installs both from the in-image `/opt/t2/repo`.  Step 5
(`images/build-installer.sh`) assembles the SD-card image: it unpacks `t2-mkfit`
from the fetched `t2-utils` package, assembles the boot FIT with it, and writes
the card.

**Step 3 must precede step 5.**  Step 5 puts `build/initramfs.gz` into the boot
FIT.  The kernel Image does **not** embed the initramfs: the boot FIT carries it
as a ramdisk subimage (kernel + device tree + ramdisk), assembled by `t2-mkfit`
(shipped by the `t2-utils` package, so the image build and the board use one
implementation).  The ramdisk is stored uncompressed because this U-Boot does
not decompress a FIT ramdisk.  The FIT's default configuration selects all three
subimages.  Because the same bytes are the `t2-initramfs` package payload, an
on-board kernel upgrade can assemble an identical FIT.

All build output goes to `build/`, which git ignores.  Finished artefacts go to
`build/out/`.

Kernel and U-Boot build timings belong to their own repositories; the rootfs
section below records this repository's.

## Rebuilding one step

`build-all.sh` can run a subset of the five steps:

```sh
./build-all.sh --only 4,5     # rootfs and installer image only
./build-all.sh --from 4       # rootfs and installer image
./build-all.sh --to 3         # components, firmware, initramfs
./build-all.sh --skip 4       # everything but the rootfs build
./build-all.sh --list         # the steps and their commands
```

Steps 3, 4 and 5 record a sha256 of their inputs under `build/.stamps/`, so a
re-run whose inputs are unchanged prints
`[skip] 4/5 rootfs: unchanged (build/.stamps/step4-rootfs.sha256)` and rebuilds
nothing.  The stamps hash file *content*, so editing a file changes a stamp but
a bare `touch` does not.  Step 1 verifies the fetched component artefacts on
every run (a cheap hash) and step 2 is a fast no-network check, so neither is
stamped.  To force one step, delete its stamp (or edit one of its inputs):

| Step | Stamp | Inputs |
|---|---|---|
| 3 | `step3-initramfs.sha256` | the `rootfs/initramfs/` tree (its packed output is the boot FIT's ramdisk and the `t2-initramfs` payload) |
| 4 | `step4-rootfs.sha256` | the rootfs scripts, the `rootfs/profiles/t2-base` tree, `rootfs/firmware/`, `rootfs/initramfs/firmware/`, `lib/`, the fetched component artefacts and `build/initramfs.gz` |
| 5 | `step5-installer.sha256` | `images/` tools and the config template, the staged kernel and boot-chain artefacts, `build/initramfs.gz`, `rootfs.ext4.zst` (plus `Image.old`/`u-boot-initial-env` when present) |

Step 5 consumes the boot-chain artefacts and the rootfs image; a component bump
changes step 4's inputs (the fetched artefacts) as well as step 5's, so both
re-run.

### Rootfs stage reuse

Step 4 (`rootfs/build.sh` -> `t2-distro.py`) keeps its working tree in
`build/rootfs/stage` and no longer wipes it on every run.  Each stage that
modifies the tree records a stamp in `build/rootfs/stage/.t2-stamps/`, and a
re-run only rebuilds the stages whose inputs changed:

| Stage | Stamp | Stale when |
|---|---|---|
| base | the pinned base tarball sha256 | the tarball in `profile/base.json` changes |
| packages | the sha256 of `packages.txt` | a package is added, removed or renamed |
| overlay | the hash of the profile `overlay/` tree | an overlay file is edited, added or removed |
| debs | the fetched `t2-utils` deb, `build/initramfs.gz` and the `t2-initramfs` version | a component bump changes the t2-utils deb, the initramfs is rebuilt, or the profile's base release changes the `t2-initramfs` version |
| hooks | the hash of the hook scripts, the hook environment and the firmware tree | a hook is edited, or the firmware changes |
| modules | the fetched `linux-modules-<rel>-t2` deb (`Version`, sha256) and the release baked into `build/out/Image` | a kernel component bump changes the deb or the Image |

A rebuilt stage cascades into the stages that read its result: a packages
rebuild re-runs overlay, debs and hooks; an overlay rebuild re-runs debs and
hooks; a debs rebuild re-runs hooks; a changed base tarball wipes the whole
stage, so every stage re-runs (including the 35 package apt install).  The
stamps are lifted out around `mke2fs`, so they never land in the image.

To force a full rootfs rebuild, delete the stage tree (the stamps go with it):

```sh
rm -rf build/rootfs/stage        # next rootfs build re-extracts and re-apts
rm -f build/.stamps/step4-rootfs.sha256   # make build-all re-run step 4
```

A rootfs build that adopts an existing stage starts at the first stale stage and
still ends with `build/out/rootfs.ext4` + `.zst`, and the file-based checks
(`verify`) still run and must pass.

Measured 2026-10-03: with every stage stamp matching, `rootfs/build.sh` rebuilt
the image in **13.6 s wall** (t2-distro 10.9 s) and all 39 checks passed.  A run
that had to rebuild overlay + hooks + modules (base and packages adopted from
the previous build's manifest) took **106 s wall**.  A full `build-all.sh` whose
inputs were all unchanged - steps 3, 4 and 5 all stamped - finished in
**1.4 s wall**.

## The board packages

Two Debian packages ship in the image, and step 4 installs both from a flat apt
repository inside the image at `/opt/t2/repo`:

* **`t2-utils`** — the board userspace (systemd units, `/etc` drop-ins,
  `/usr/local/sbin` helpers).  It is built by `zspace-t2-ubuntu-utils`; this
  repository consumes it as an artefact, and `components.lock` pins the `.deb`
  by sha256.  It also carries `t2-mkfit`, the boot-FIT assembler the image build
  and the board both use.
* **`t2-initramfs`** — the boot initramfs, built here by
  `rootfs/initramfs/package.sh` from `build/initramfs.gz` (the same bytes as the
  FIT ramdisk).  It installs `/boot/initramfs-t2.gz`.  `/boot` is an ordinary
  rootfs directory on the board (the profile's fstab carries no entries and the
  boot FAT is mounted on demand by `t2-utils`' `t2-boot-commit.sh`), so nothing
  shadows the file.

Step 4 drops both `.deb`s into `/opt/t2/repo` with a generated `Packages` index,
writes `/etc/apt/sources.list.d/t2.list` (`deb [trusted=yes] file:/opt/t2/repo
./`) and installs them *from that repo* in the chroot.  The `/etc` files are
conffiles, and `t2-utils`' `postinst` re-enables the units idempotently without
ever failing the dpkg run.  The `t2-initramfs` version is the profile's base
release (`base.json` `release`, sanitised).

A running board can therefore reinstall or upgrade the packages offline:

```sh
apt-get update && apt-cache policy t2-utils
apt-get install --reinstall -y t2-utils      # or: apt-get upgrade
```

A newer component applies without reflashing: copy its `.deb` to the board and
`apt-get install -y /path/t2-utils_<version>_all.deb` (apt resolves the
`Depends` against what is already installed).  `rootfs/README.md` has the
details for the image's packages.

An **on-board kernel upgrade** works the same way: `linux-image-<rel>-t2`
depends on `t2-initramfs`, and its `postinst` assembles a boot FIT from the
kernel, the board device tree and `/boot/initramfs-t2.gz` with `t2-mkfit`, then
installs it into the boot tree.  The boot tree is addressed by `T2_BOOT_DIR`;
when it is unset (the image-build chroot, and every board today) the package
skips the FIT with a clear message and exits 0.

## Firmware

The Broadcom WiFi and Bluetooth firmware for the AP6275P module comes from the
vendor rootfs.  No redistributable source ships it: it is not in
`linux-firmware`, and the Ubuntu firmware packages do not carry it.  The four
vendor blobs are committed in `rootfs/firmware/brcm/` and embedded in the
images: step 3 packs them into the initramfs (the boot FIT's ramdisk) and the
rootfs hook installs them in `/lib/firmware`.  `rootfs/fetch.sh` verifies them
and writes the mainline names; it can also refresh them from a T2 that still
runs the vendor firmware, or from a vendor update package.
`rootfs/build.sh` stops with a clear message when the firmware is missing.

The mainline driver expects these names in `/lib/firmware/brcm/`:

| Mainline name | Vendor blob |
|---|---|
| `brcmfmac43752-pcie.bin` | `fw_bcm43752a2_pcie_ag.bin` |
| `brcmfmac43752-pcie.txt` | `nvram_AP6275P.txt` |
| `brcmfmac43752-pcie.clm_blob` | `clm_bcm43752a2_ag.blob` |

Only the `.bin` is mandatory.  The module can enumerate before the root
filesystem is switched in, so the files are packed into the **initramfs** as
well as installed in `/lib/firmware`; without them the load fails with
`-ENOENT`.

The RTL8156B Ethernet firmware (`rtl_nic/rtl8156b-2.fw`) and the wireless
regulatory database are redistributable, so the build fetches those normally.

## Docker

`Dockerfile` pins the host tools above, and `docker-build.sh` runs the whole
build inside it - the same five steps, through `build-all.sh`:

```sh
./docker-build.sh                    # the whole build -> build/out/installer.img
./docker-build.sh rootfs/build.sh    # one step, in the same environment
./docker-build.sh bash               # a shell in the same environment
```

The image carries no repository content: the tree is bind-mounted at `/work`,
the container runs with your uid and gid, so `build/` stays yours, and the only
things baked in are Ubuntu 24.04 and the packages listed above (it also carries
the component repositories' toolchains, which this build does not use).  The
arch-specific target headers are selected when the
image is built, from the image's own architecture (`dpkg --print-architecture`).
The default tag is `zspace-t2-build:$(uname -m)`, so an image built on one
architecture is never run on the other; `IMAGE=` overrides the tag.  The apt
lists are left in the image on purpose - on x86-64 hosts `rootfs/t2-distro.py`
fetches `qemu-user-static` with `apt-get download`, and the lists let it
resolve the version.  On arm64 the image installs `proot` directly (see the
notes above).  Build the image once; rebuild it (`docker build --no-cache`)
when those lists go stale, because the download resolves the version from
them.

Caveats that remain:

* **Network.**  Step 1 downloads the pinned component artefacts, step 3
  downloads the BusyBox source, the rootfs build unpacks the Ubuntu base tarball
  and collects its chroot tools (`qemu-user-static` and `proot` on x86-64 hosts;
  on arm64 `proot` is already installed), and the chroot apt-installs the
  profile.  All of it is cached under `build/`, so a second run only
  re-downloads inside the chroot.
* **`ptrace`.**  `docker-build.sh` passes `--security-opt seccomp=unconfined`:
  the default rootfs backend is `proot`, which needs `ptrace`, and Docker's
  default seccomp profile denies it.
* **Privilege.**  Unchanged from a host build: no loop devices and no host root;
  the image stage is `fakeroot` + `mke2fs -d`, and `qemu-user-static` is
  unpacked rather than bound into `binfmt_misc`.
* **Devices.**  The container is only the build.  The serial console, maskrom
  and the gadget link in `tools/` stay on the host and need USB access there.

## Hardware checks still pending

The build verifies the image by structure and content: the partition table, the
boot tree, the FIT's kernel + device-tree + ramdisk subimages, and the mountable
rootfs.  Two things can only be confirmed on the board:

* **The FIT ramdisk boot path.**  U-Boot must hand the FIT's ramdisk subimage to
  the kernel when the generated `extlinux.conf` carries no `INITRD` line.  It
  deliberately has none, so `booti` uses the FIT configuration's ramdisk
  (checked against U-Boot's `boot/pxe_utils.c` and `boot/image-board.c`), but no
  board has booted this exact FIT yet.
* **The eMMC write.**  The installer's repartition and payload write is
  exercised against files, not a real eMMC; the first full install on hardware
  is still the real test.

Until those are run, treat them as unverified even when the build reports the
image is structurally correct.
