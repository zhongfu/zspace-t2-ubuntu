# ZSpace T2 — mainline Linux and Ubuntu

Build Ubuntu for the ZSpace T2 network storage device (Rockchip RK3568). The
build uses mainline Linux with a small patch set, mainline U-Boot, and a stock
Ubuntu ARM64 rootfs with our board support files.

The build is split across four repositories. This repository owns the
initramfs, the installer, the rootfs and the image assembly. The kernel, U-Boot
and the board userspace (`t2-utils`) come from `zspace-t2-kernel`,
`zspace-t2-bootloader` and `zspace-t2-ubuntu-utils` as released artefacts;
`components.lock` pins every one of them by sha256, so a build here always
assembles one exact set of components.

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
| `components.lock` | the component releases and sha256s this image is built from |
| `rootfs/` | Ubuntu profile, the boot initramfs, firmware, rootfs build script |
| `images/` | boot tree and installer image tools |
| `lib/` | shared Python modules (`t2lib.py`, `rkimg.py`) |
| `tools/` | helpers for a live T2, and `components.py`, which fetches the pinned artefacts |
| `docs/` | tinkering, building, using, releasing |

The kernel, U-Boot and `t2-utils` sources do not live here: the three component
repositories are read-only inputs to this build.

## Build

You need a Linux x86-64 host, about 40 GB free, and the tools listed in
`docs/building.md`. The build is five steps:

```sh
./build-all.sh --only 1            # 1 verify, fetch and stage the pinned components
rootfs/fetch.sh                    # 2 verify the committed WiFi/BT firmware
rootfs/initramfs/build.sh          # 3 -> build/initramfs, build/initramfs.gz
rootfs/build.sh                    # 4 -> build/out/rootfs.ext4.zst
images/build-installer.sh          # 5 -> build/out/installer.img
```

Step 1 is `tools/components.py fetch` plus the staging `build-all.sh` does
around it: the fetched raw kernel and boot-chain files are copied into
`build/out/`, where the later steps read every input.

`components.lock` pins the kernel, U-Boot and `t2-utils` artefacts by sha256;
step 1 fetches them from the component releases and stops on a mismatch, so an
image can never silently mix component versions. To build offline, point
`T2_COMPONENTS_DIR` at a directory holding the locked artefacts.

The kernel Image does not embed the initramfs. Each boot FIT carries one as a
ramdisk subimage (kernel + device tree + ramdisk); both are assembled by
`t2-mkfit` from the `t2-utils` package: the card's `/Image` carries the
installer ramdisk, while the eMMC's `/Image` is built from `/Image.emmc` and
carries the initramfs-tools image (`/boot/initrd.img-<rel>`). A FIT
configuration selects one ramdisk, and bootstd only boots the default one, so
the two roles need two files. Because the installer ramdisk stays separate, a
change to the installer or the rootfs does not force a kernel rebuild. This
repository also builds the `t2-initramfs` package (the installer ramdisk,
`/boot/initramfs-t2.gz`), which the kernel postinst falls back to when a rootfs
has no initramfs-tools.

`docs/building.md` is the full reference. With Docker, `./docker-build.sh`
builds everything. On a host, `./build-all.sh` runs the five steps, or a subset
with `--only`, `--from`, `--to`, and `--skip`; unchanged steps are skipped.

Then write `installer.img` to an SD card and confirm the install with the
front-panel button. See `docs/using.md`.

## Firmware

The AP6275P WiFi and Bluetooth firmware comes from the vendor rootfs. No
redistributable source ships it: it is not in `linux-firmware`, and the Ubuntu
firmware packages do not carry it. The four blobs are committed under
`rootfs/firmware/brcm/` and embedded in the images. `rootfs/fetch.sh` verifies
them, and can refresh them from a T2 or a vendor update package. Everything else
the images need is redistributable or built from source.

## Documentation

* `docs/tinkering.md` — open the case, serial console, maskrom button, SD boot.
* `docs/building.md` — host setup, build order, Docker.
* `docs/using.md` — install, provisioning, backup.
* `docs/releasing.md` — build the artefacts on a tag and publish a release.

`tools/README.md` lists the helper scripts. Each top-level directory has its own
`README.md`.

## Licence

Kernel patches and device trees carry the kernel's licence (GPL-2.0), and the
component repositories carry it as their own. The build scripts and
documentation here are published as-is, with no separate licence grant.
