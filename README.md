# ZSpace T2 — mainline Linux and Ubuntu

Build Ubuntu for the ZSpace T2 network storage device (Rockchip RK3568):
mainline Linux plus a small patch set, mainline U-Boot, and a stock Ubuntu
ARM64 rootfs with our board support files.

## What works

| Feature | State |
|---|---|
| Boot | eMMC or SD card, with an A/B kernel fallback in U-Boot |
| Storage | eMMC, SD, M.2 NVMe, USB 3 |
| Network | RTL8156B 1 Gbit Ethernet, AP6275P WiFi (BCM43752, PCIe), USB-C gadget link |
| Bluetooth | BCM43752 over UART, LE advertising |
| Video | HDMI console, RK3568 VDPU346 decoders (patch set) |
| Thermal | CPU and GPU zones, the fan as a cooling device, fan tachometer |
| Buttons and LEDs | power and reset keys, HDD and power LEDs |

Not included: the vendor's proprietary applications (zfilev2, zalbumv2, znvr,
...), and the NPU and ISP userspace.

## Layout

| Path | Contents |
|---|---|
| `kernel/` | kernel patches, the kernel config, fetch and build scripts |
| `u-boot/` | board defconfigs, board device trees, one host patch, fetch and build scripts |
| `rootfs/` | Ubuntu profile (packages, overlay files, hooks), the initramfs, firmware fetch, build script |
| `images/` | boot tree and installer image tools |
| `lib/` | Python modules shared by the tools above |
| `tools/` | helpers for a live T2: serial console, GPIO, gadget link, flash, image inspection |
| `docs/` | tinkering, building, using |

## Build

Requirements: a Linux x86-64 host, ~40 GB of free space, and the tools listed
in `docs/building.md`.  Run the steps in this order.

```sh
rootfs/fetch.sh                             # vendor WiFi/BT firmware (see below)
rootfs/initramfs/build.sh                   # busybox -> build/initramfs
kernel/fetch.sh    &&  kernel/build.sh      # -> build/out/Image, rk3568-t2.dtb, modules/
u-boot/fetch.sh    &&  u-boot/build.sh      # -> build/out/u-boot.itb, idbloader.img
rootfs/build.sh                             # -> build/out/rootfs.ext4.zst
images/build-installer.sh                   # -> build/out/installer.img
```

The kernel embeds the initramfs tree, so build the initramfs before the
kernel.  All output goes to `build/`, which git ignores.

With Docker it is one command: `./docker-build.sh` builds everything in a
container that pins the tools above (see `docs/building.md`).  On a host with
those tools installed, `./build-all.sh` runs the same eight steps.

Then write `installer.img` to an SD card, insert it, and confirm the install
with the front-panel button.  See `docs/using.md`.

## Firmware

The Broadcom WiFi and Bluetooth firmware for the AP6275P module comes from the
vendor rootfs and is not redistributable, so it is not in this repository.
`rootfs/fetch.sh` copies it from a T2 that still runs the vendor firmware, or
from a vendor update package.  The rootfs build stops with a clear message
when the firmware is missing.  Everything else the images need (RTL8156B
firmware, the wireless regulatory database, busybox) is redistributable or
built from source.

## Documentation

* `docs/tinkering.md` — open the case, serial console, maskrom button, SD boot.
* `docs/building.md` — set up the host, build each component, notes on Docker.
* `docs/using.md` — install the image, provision the board, back up the eMMC.

`tools/README.md` lists the helper scripts, and each top-level directory has
its own `README.md`.

## Licence

Kernel patches and device trees carry the kernel's licence (GPL-2.0).  The
build scripts and documentation are published as-is, with no separate licence
grant.
