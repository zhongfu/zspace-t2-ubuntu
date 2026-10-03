# ZSpace T2 — mainline Linux and Ubuntu

Build Ubuntu for the ZSpace T2 network storage device (Rockchip RK3568). The
build uses mainline Linux with a small patch set, mainline U-Boot, and a stock
Ubuntu ARM64 rootfs with our board support files.

## What works

| Feature | State |
|---|---|
| Boot | eMMC or SD card, with an A/B kernel fallback in U-Boot |
| Storage | eMMC, SD, M.2 NVMe, USB 3 |
| Network | RTL8156B 1 Gbit Ethernet, AP6275P WiFi (BCM43752, PCIe), USB-C gadget link |
| Bluetooth | BCM43752 over UART, LE advertising |
| Video | HDMI console, RK3568 VDPU346 decoders (patch set) |
| Thermal | CPU and GPU thermal zones, the fan as a cooling device, fan tachometer |
| Buttons and LEDs | power and reset keys, HDD and power LEDs |

Not included: the vendor's proprietary applications (zfilev2, zalbumv2, znvr,
...), the NPU, and ISP userspace.

## Layout

| Path | Contents |
|---|---|
| `kernel/` | kernel patches, config, fetch and build scripts |
| `u-boot/` | board defconfigs, device trees, patches, fetch and build scripts |
| `rootfs/` | Ubuntu profile, initramfs, firmware fetch, build script |
| `images/` | boot tree and installer image tools |
| `lib/` | shared Python modules |
| `tools/` | helpers for a live T2 |
| `docs/` | tinkering, building, using |

## Build

You need a Linux x86-64 host, about 40 GB free, and the tools listed in
`docs/building.md`. Build the initramfs before the kernel: it is embedded.

```sh
rootfs/fetch.sh                             # vendor WiFi/BT firmware
rootfs/initramfs/build.sh                   # -> build/initramfs
kernel/fetch.sh    &&  kernel/build.sh      # -> build/out/Image, rk3568-t2.dtb, modules/
u-boot/fetch.sh    &&  u-boot/build.sh      # -> build/out/u-boot.itb, idbloader.img
rootfs/build.sh                             # -> build/out/rootfs.ext4.zst
images/build-installer.sh                   # -> build/out/installer.img
```

`docs/building.md` is the full reference. With Docker, `./docker-build.sh`
builds everything. On a host, `./build-all.sh` runs the eight steps, or a subset
with `--only`, `--from`, `--to`, and `--skip`; unchanged steps are skipped.

Then write `installer.img` to an SD card and confirm the install with the
front-panel button. See `docs/using.md`.

## Firmware

The AP6275P WiFi and Bluetooth firmware comes from the vendor rootfs and is not
redistributable. `rootfs/fetch.sh` copies it from a T2 that runs the vendor
firmware, or from a vendor update package. Everything else the images need is
redistributable or built from source.

## Documentation

* `docs/tinkering.md` — open the case, serial console, maskrom button, SD boot.
* `docs/building.md` — host setup, build order, Docker.
* `docs/using.md` — install, provisioning, backup.
* `docs/releasing.md` — build the artefacts on a tag and publish a release.

`tools/README.md` lists the helper scripts. Each top-level directory has its own
`README.md`.

## Licence

Kernel patches and device trees carry the kernel's licence (GPL-2.0). The build
scripts and documentation are published as-is, with no separate licence grant.
