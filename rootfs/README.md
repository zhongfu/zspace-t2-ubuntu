# Ubuntu rootfs

This subtree builds a stock Ubuntu ARM64 rootfs for the ZSpace T2 with the
board support files the bring-up needs.  It has three parts:

| Path | Contents |
|---|---|
| `profiles/t2-base/` | the profile: the Ubuntu base tarball, the package list, the config example and the build hooks |
| `initramfs/` | the bring-up initramfs source (BusyBox, `init`, the card installer, firmware, build script and the `t2-initramfs` packaging script) |
| `firmware/` | the committed vendor WiFi/BT blobs and the script that verifies them |

`t2-distro.py` turns a profile into `build/rootfs/rootfs.ext4` (and the
zstd-compressed `.zst`); `build.sh` runs the whole flow.  The shared helpers live
once at `<repo>/lib/` (`lib/t2lib.py`, `lib/rkimg.py`); `rootfs/` imports them
from its own location.

The board userspace (`t2-utils`) is not built here any more:
`zspace-t2-ubuntu-utils` builds it and `components.lock` pins the `.deb` by
sha256.  `t2-distro.py` installs the fetched package, plus the `t2-initramfs`
package this subtree builds, from the in-image apt repository (below).

## The profile

`profiles/t2-base/` is data, not code:

* `base.json` - the pinned Ubuntu base tarball (26.04.1 "resolute", arm64) and
  its SHA-256.  The build extracts it and installs on top.
* `packages.txt` - one dpkg package per line, with the reason for each group in
  comments.  The base tarball ships no service manager, so `systemd` +
  `systemd-sysv` are load-bearing; the rest is ssh, DHCP, WiFi, storage tools,
  the A/B boot helpers and the BLE management stack.
* `image.json` - the artifact (`rootfs.ext4`), its ext4 label and pinned UUID,
  the target partition geometry, the kernel `cmdline`, the firmware source tree
  (`build/firmware`, staged by `build.sh`) and the boot FITs' inputs (kernel,
  DTB and ramdisk), which `images/build-installer.sh` assembles with `t2-mkfit`
  into the card's `/Image` and the eMMC's `/Image.emmc`.
* `t2-config.example` - the documented `t2-config` keys for out-of-band
  provisioning.  Copy it to a FAT partition labelled `T2-CONFIG`.
* `hooks/` - scripts run in the chroot, ascending, after the packages.

### hooks/

* `10-apt.sh` - `apt-get update`.
* `15-purge.sh` - removes recommender-pulled daemons (modemmanager) and proves
  the load-bearing packages survived.
* `20-basics.sh` - hostname, empty machine-id, timezone, an fstab with no root
  line, and no `/etc/network/interfaces`.
* `30-services.sh` - enables the `t2-utils` units and masks boot-time services
  this board never needs (`NetworkManager-wait-online`, ext4 scrub, NVMe-oF,
  netplan-configure).
* `40-console.sh` - serial (ttyS2, 1500000 baud) and HDMI (tty1) logins, a
  documented static root password, no autologin.
* `50-firmware.sh` - copies the WiFi/BT and RTL8156B firmware from
  `/t2-profile/firmware` into `/lib/firmware`.
* `60-ssh.sh` - key-only root ssh and no baked host keys.
* `70-leds.sh`, `75-provision.sh` - assert the `t2-utils` helper scripts are
  executable.
* `80-growroot.sh` - enables the grow unit and checks `growpart`/`resize2fs`.
* `90-cleanup.sh` - `apt-get clean` and removes the build-time policy hook.

## The in-image apt repository

The `debs` stage of `t2-distro.py` assembles two packages into a flat apt
repository inside the image at `/opt/t2/repo` (with a generated `Packages`
index), writes `/etc/apt/sources.list.d/t2.list`:

```
deb [trusted=yes] file:/opt/t2/repo ./
```

and installs them *from that repository* in the chroot:

* **`t2-utils`** - the board userspace (systemd units, `/etc` drop-ins,
  `/usr/local/sbin` helpers, and `t2-mkfit`).  Built by
  `zspace-t2-ubuntu-utils`; the fetched `.deb` comes from
  `build/components/utils/`, sha256-verified against `components.lock`.
* **`t2-initramfs`** - the installer initramfs, built here by
  `initramfs/package.sh` from `build/initramfs.gz`.  It installs
  `/boot/initramfs-t2.gz`, the card FIT's ramdisk and the fallback
  `linux-image-<rel>-t2` uses when a rootfs has no initramfs-tools (its postinst
  otherwise builds the boot FIT from `/boot/initrd.img-<rel>`).

So the image ships both installed packages and the repository they came from,
and every build exercises the offline path a running board uses (`trusted=yes`
is what lets apt use the repo without a Release signature; it carries the
image's own packages, not a third-party feed).

The `t2-initramfs` version is the profile's base release (`base.json`
`release`, sanitised), so `apt-get upgrade` only supersedes an installed
`t2-initramfs` once that release changes.  A same-version rebuild reaches the
image through the `debs` stage's `--reinstall`; on a board, install the new
`.deb` file directly (below).

On a running board, the same-version reinstall or an upgrade to a version the
index lists works with no network:

```sh
apt-get update
apt-cache policy t2-utils
apt-get install --reinstall -y t2-utils      # or: apt-get upgrade
```

For a *newer* component build without reflashing, copy its `.deb` to the board
and install the file directly (apt resolves its dependencies against what is
already installed, so no repository refresh is needed):

```sh
scp t2-utils_26.04.2_all.deb root@<board>:/tmp/
ssh root@<board> 'apt-get install -y /tmp/t2-utils_26.04.2_all.deb'
```

## initramfs

`initramfs/` is the source of the installer initramfs: the card boots it, and
it runs the installer that writes the eMMC.  The kernel Image does **not**
embed it: `rootfs/initramfs/build.sh` packs the tree into
`build/initramfs.gz`, which `images/build-installer.sh` puts into the card FIT
as its ramdisk subimage, and `initramfs/package.sh` turns the same bytes into
the `t2-initramfs` package.  `/init` mounts `/` and `switch_root`s straight to
systemd; on the failure path it brings up a recovery network.  With
`t2.mode=flash` it runs `installer.sh`, the card installer that writes the eMMC.
`bin/t2-keywait` (from `src/t2-keywait.c`) is the installer's exclusive
power-button counter.

`busybox.config` is the BusyBox 1.36.1 configuration (static aarch64,
`CONFIG_TC` off).  `initramfs/build.sh` downloads and verifies that source,
builds it, generates every applet symlink, compiles `t2-keywait`, lays out
`build/initramfs/` with the firmware tree, and packs `build/initramfs.gz`.  The
BusyBox binary and the symlinks are build outputs, never committed.  The
redistributable `rtl_nic/*.fw` and `regulatory.db*` are committed under
`initramfs/firmware/`; the Broadcom blobs come from `firmware/` (see below).

## Firmware

The AP6275P module (Broadcom BCM43752) needs firmware that no redistributable
source ships: it is not in `linux-firmware`, and the Ubuntu firmware packages do
not carry it.  The four vendor blobs are committed in `firmware/brcm/` and
embedded in the images.  `fetch.sh` verifies every SHA-256 (documented in
`firmware/README.md`), writes the mainline `brcmfmac` names alongside the vendor
ones, and can refresh the blobs from a T2 that runs the vendor firmware, from a
vendor `.zspace` OTA package, or from a directory that already holds them.  The
rootfs build stops with a clear message when the firmware is missing.
Everything else is committed (the RTL NIC blobs, the regulatory database) or
built from source (BusyBox).

## Build

Requirements: a Linux x86-64 host, the `aarch64-linux-gnu-` cross toolchain,
`python3`, `zstd`, `cpio`, and the packages `t2-distro.py` checks (tar, curl,
fakeroot, `mke2fs`, `debugfs`, `e2fsck`).  No root is needed: the build uses
proot + `qemu-user-static`, fetched into `build/`.

Build order: **components fetch -> firmware verify -> initramfs -> rootfs ->
installer image**.  The rootfs step reads the fetched component artefacts
(`build/components/`) and `build/initramfs.gz`, so both must exist first; the
kernel and U-Boot themselves are built by their own repositories.

```sh
./build-all.sh --only 1                         # fetch, verify and stage the components
rootfs/fetch.sh                                 # verify the committed firmware
rootfs/initramfs/build.sh                       # -> build/initramfs(.gz)
rootfs/build.sh                                 # -> build/out/rootfs.ext4(.zst)
```

`build.sh` builds the initramfs when `build/initramfs` is missing, stages the
firmware tree, runs `t2-distro.py`, and writes `build/out/rootfs.ext4` plus the
zstd-compressed `build/out/rootfs.ext4.zst` that `images/build-installer.sh`
puts into the installer payload.  It also writes the `t2-initramfs` package into
`build/out/`.

`build.sh` reuses `build/rootfs/stage` instead of wiping it: each stage that
changes the tree stamps its inputs under `build/rootfs/stage/.t2-stamps/`, so a
rerun rebuilds only the stale stages.  A changed base tarball wipes the whole
stage - stamps and all - forcing a full rebuild; `rm -rf build/rootfs/stage`
does the same by hand.  `docs/building.md` lists what makes each stage stale.

Useful flags: `--dry-run`, `--rebuild-initramfs`, `--no-zstd`,
`--firmware-from-host/--firmware-ota/--firmware-from-dir`.  Each script also
supports `-h/--help`.

A cheap plan-only check:

```sh
python3 rootfs/t2-distro.py --profile rootfs/profiles/t2-base \
    --out build/rootfs --dry-run
```
