# Ubuntu rootfs

This subtree builds a stock Ubuntu ARM64 rootfs for the ZSpace T2 with the
board support files the bring-up needs.  It has three parts:

| Path | Contents |
|---|---|
| `profiles/t2-base/` | the profile: the Ubuntu base tarball, the package list, the overlay files and the build hooks |
| `initramfs/` | the bring-up initramfs source (BusyBox, `init`, the card installer, firmware, build script) |
| `firmware/` | where `fetch.sh` puts the non-redistributable vendor WiFi/BT blobs |

`t2-distro.py` turns a profile into `build/rootfs/rootfs.ext4`; `build.sh` runs
the whole flow.  The shared pipeline code lives once at `<repo>/lib/`
(`lib/t2-build.py`, `lib/rkimg.py`); `rootfs/` imports it from its own location.

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
  (`build/firmware`, staged by `build.sh`) and the boot-FIT freshness check.
* `t2-config.example` - the documented `t2-config` keys for out-of-band
  provisioning.  Copy it to a FAT partition labelled `T2-CONFIG`.
* `overlay/` - files copied verbatim into the image (modes preserved).
* `hooks/` - scripts run in the chroot, ascending, after the packages.

### overlay/

Systemd units (enabled by `hooks/30-services.sh` unless noted):

* `t2-ble.service` - BlueZ GATT server for out-of-band management over BLE.
* `t2-boot-commit.service` - clears `bootcount`/`upgrade_available` in the
  U-Boot environment once a boot worked, and promotes `/Image.old` after a
  fallback boot.
* `t2-firstboot-identity.service` - derives the per-board machine-id/hostname
  on first boot; runs before provisioning so a card `hostname=` overrides it.
* `t2-growroot.service` - grows the root filesystem to the end of its partition
  on first boot (the artifact is built smaller than the partition).
* `t2-hddled.service` - drives the per-bay HDD present/activity/fault LEDs.
* `t2-leds.service` - drives the power LEDs (green activity, red fault).
* `t2-powerkey.service` - power-button policy: short press ignored, a held press
  asks for a graceful poweroff.
* `t2-provision.service` - applies the `t2-config` FAT partition keys on boot.
* `t2-ssh-hostkeys.service` - generates SSH host keys on first boot (the image
  ships none).
* `t2-usbgadget.service` - brings up the USB device-mode gadget (NCM network +
  the config partition as a USB drive).

Drop-ins and configuration:

* `etc/NetworkManager/conf.d/10-t2-manage-ethernet.conf` - lets NetworkManager
  manage the ethernet devices (Ubuntu's default leaves them unmanaged).
* `etc/NetworkManager/conf.d/20-t2-usb-gadget.conf` - keeps NetworkManager off
  `usb0`, which the board serves DHCP on itself.
* `etc/dnsmasq.d/t2-usbgadget.conf` - DHCP server for the gadget link only.
* `etc/ssh/sshd_config.d/10-t2.conf` - root ssh with key *and* password.
* `etc/systemd/logind.conf.d/10-t2.conf` - takes the power key away from logind.
* `etc/systemd/system.conf.d/10-t2.conf` - feeds the RK3568 hardware watchdog.
* `etc/t2/powerkey.conf` - `t2-powerkey.py` input device, key code and hold time.
* `etc/default/t2-hddled` - bay-to-block-device map and poll interval.
* `etc/fw_env.config` - where U-Boot's persistent environment file lives.
* `etc/u-boot-initial-env` - the initial U-Boot environment (A/B fallback).

Helpers in `/usr/local/sbin/`:

* `t2-ble.py`, `t2-ble-password` - the BLE GATT application and its key helper.
* `t2-boot-commit.sh` - the A/B commit script.
* `t2-firstboot-identity.sh` - the first-boot identity script.
* `t2-growroot.sh` - the root-filesystem grow script.
* `t2-hddled.sh`, `t2-leds.sh` - the two LED owners.
* `t2-powerkey.py` - the power-button measurement daemon.
* `t2-provision.sh` - applies the config partition.
* `t2-sethostname` - sets the hostname and keeps `/etc/hosts` in step.
* `t2-usbgadget.sh` - the USB gadget supervisor.

### hooks/

* `10-apt.sh` - `apt-get update`.
* `15-purge.sh` - removes recommender-pulled daemons (modemmanager) and proves
  the load-bearing packages survived.
* `20-basics.sh` - hostname, empty machine-id, timezone, an fstab with no root
  line, and no `/etc/network/interfaces`.
* `30-services.sh` - enables the units above and masks boot-time services this
  board never needs (`NetworkManager-wait-online`, ext4 scrub, NVMe-oF,
  netplan-configure).
* `40-console.sh` - serial (ttyS2, 1500000 baud) and HDMI (tty1) logins, a
  documented static root password, no autologin.
* `50-firmware.sh` - copies the WiFi/BT and RTL8156B firmware from
  `/t2-profile/firmware` into `/lib/firmware`.
* `60-ssh.sh` - key-only root ssh and no baked host keys.
* `70-leds.sh`, `75-provision.sh` - assert the helper scripts are executable.
* `80-growroot.sh` - enables the grow unit and checks `growpart`/`resize2fs`.
* `90-cleanup.sh` - `apt-get clean` and removes the build-time policy hook.

## initramfs

`initramfs/` is the source of the initramfs the kernel embeds.  `/init` mounts
`/` and `switch_root`s straight to systemd; on the failure path it brings up a
recovery network.  With `t2.mode=flash` it runs `installer.sh`, the card
installer that writes the eMMC.  `bin/t2-keywait` (from `src/t2-keywait.c`) is
the installer's exclusive power-button counter.

`busybox.config` is the BusyBox 1.36.1 configuration (static aarch64,
`CONFIG_TC` off).  `initramfs/build.sh` downloads and verifies that source,
builds it, generates every applet symlink, compiles `t2-keywait`, and lays out
`build/initramfs/` with the firmware tree.  The BusyBox binary and the symlinks
are build outputs, never committed.  The redistributable `rtl_nic/*.fw` and
`regulatory.db*` are committed under `initramfs/firmware/`; the Broadcom blobs
come from `firmware/` (see below).

## Firmware

The AP6275P module (Broadcom BCM43752) needs firmware that is **not
redistributable** and not in `linux-firmware`, so it is not in this repository.
`fetch.sh` copies it from a T2 that runs the vendor firmware, from a vendor
`.zspace` OTA package, or from a directory that already holds it, verifies
every SHA-256 (documented in `firmware/README.md`), and writes the mainline
`brcmfmac` names alongside the vendor ones.  The rootfs build stops with a clear
message when the firmware is missing.  Everything else is committed (the RTL
NIC blobs, the regulatory database) or built from source (BusyBox).

## Build

Requirements: a Linux x86-64 host, the `aarch64-linux-gnu-` cross toolchain,
`python3`, `zstd`, and the packages `t2-distro.py` checks (tar, curl, fakeroot,
`mke2fs`, `debugfs`, `e2fsck`).  No root is needed: the build uses proot +
`qemu-user-static`, fetched into `build/`.

Build order: **firmware fetch -> initramfs -> kernel -> u-boot -> rootfs ->
installer image**.  The kernel embeds `build/initramfs`, so the initramfs must
exist before `kernel/build.sh`.

```sh
rootfs/fetch.sh --from-host root@192.168.1.50   # vendor firmware
rootfs/build.sh                                 # -> build/out/rootfs.ext4(.zst)
```

`build.sh` builds the initramfs when `build/initramfs` is missing, stages the
firmware tree, requires `build/kernel` (built by `kernel/build.sh`), runs
`t2-distro.py`, and writes `build/out/rootfs.ext4` plus the
zstd-compressed `build/out/rootfs.ext4.zst` that `images/build-installer.sh`
puts into the installer payload.

Useful flags: `--dry-run`, `--rebuild-initramfs`, `--no-zstd`,
`--firmware-from-host/--firmware-ota/--firmware-from-dir`.  Each script also
supports `-h/--help`.

A cheap plan-only check:

```sh
python3 rootfs/t2-distro.py --profile rootfs/profiles/t2-base \
    --out build/rootfs --dry-run
```
