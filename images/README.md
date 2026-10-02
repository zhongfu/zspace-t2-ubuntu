# images — boot tree and installer image tools

These tools assemble the card images from the artifacts the other subtrees
build.  All inputs and outputs live under `build/out/`; every tool resolves its
paths from its own location, so it runs from any clone path.

## Tools

| File | Purpose |
|---|---|
| `rk-fit.py` | pack the kernel `Image` and the board DTB into a vendor-compatible Rockchip FIT (U-Boot 2017.09 reads `data-position`, not `data-offset`) |
| `t2-boot-fat.py` | build the FAT boot tree mainline U-Boot's bootstd reads: `/Image`, the DTB, `/extlinux/extlinux.conf`, `/uboot.env`, `/Image.old` — either as a `boot.vfat` image or as files (`--out-dir`) for the card's config FAT |
| `t2-image.py` | assemble a whole-disk image: GPT at the vendor's eMMC offsets, the loader, the FIT and the rootfs, plus the optional `T2-CONFIG` FAT and `T2-FLASH` ext4 partitions |
| `build-installer.sh` | run the whole installer-card build end to end (the entry point) |

`t2-image.py` and `rk-fit.py` import `lib/rkimg.py`, the shared Rockchip parser
module at the repository root.

## Installer card layout

`build-installer.sh` writes `build/out/installer.img`.  It mirrors the vendor
eMMC geometry so the vendor loader finds the same numbers, then appends the two
installer partitions:

| Region | LBA | Size | Contents |
|---|---|---|---|
| unpartitioned | `0x40` | 512 KiB | `idbloader.img`, with guarded copies at `0x440`, `0x840`, `0xc40`, `0x1040` |
| p1 `uboot` | `0x4000` | 4 MiB | `u-boot.itb` |
| p2 `misc` | `0x6000` | 4 MiB | empty (vendor scratch; kept so the numbers below match) |
| p3 `boot` | `0x8000` | 64 MiB | the mainline kernel FIT |
| p4 `recovery` | `0x28000` | 32 MiB | empty (kept empty on purpose, same reason) |
| p5 `backup` | `0x38000` | 32 MiB | empty (kept empty on purpose) |
| p6 `rootfs` | `0x48000` | to the end | empty on the installer card (`--rootfs none`); on a normal SD card it holds `rootfs.ext4` |
| p7 `config` | after p6 | 256 MiB | FAT volume labelled **`T2-CONFIG`**: `/t2-config.txt` plus the boot tree as files — `/Image`, the DTB, `/extlinux/extlinux.conf`, `/extlinux/t2-emmc.conf`, `/uboot.env`, `/Image.old`, `/u-boot.itb`, `/idbloader.img` |
| p8 `flash` | after p7 | rootfs + slack | ext4 volume labelled **`T2-FLASH`** holding `/rootfs.ext4.zst` |

The card's own boot chain is the FIT at LBA `0x8000` plus the bootstd tree on
`T2-CONFIG`; `extlinux.conf` selects the `t2-installer` entry (`t2.mode=flash`),
so the initramfs runs the installer and writes the eMMC.  `t2-emmc.conf` is the
descriptor the installer promotes to the eMMC's `/extlinux/extlinux.conf`.
`/t2-config.txt` arms the flash flow (`flash.presses`, `flash.window`,
`flash.sha256`, …); see `t2-install-card-config.example.txt`.

## Build

Requirements: `python3`, `mtools`, `mke2fs`/`debugfs`, `blkid`, `dtc`
(`device-tree-compiler` or `$DTC`), and a POSIX shell.

```sh
kernel/fetch.sh && kernel/build.sh    # -> build/out/Image, rk3568-t2.dtb
u-boot/fetch.sh && u-boot/build.sh    # -> build/out/u-boot.itb, idbloader.img
rootfs/fetch.sh && rootfs/build.sh    # -> build/out/rootfs.ext4.zst
images/build-installer.sh             # -> build/out/installer.img
```

Then write the card:

```sh
dd if=build/out/installer.img of=/dev/sdX bs=4M conv=sparse
```

`build-installer.sh --config FILE` uses a different `/t2-config.txt` template.

## Other images

A normal SD card that boots the rootfs on the card itself (no installer):

```sh
images/t2-boot-fat.py --image build/out/Image --dtb build/out/rk3568-t2.dtb \
    --out build/out/boot.vfat
images/t2-image.py --out build/out/t2-base-sd.img --size 3.6G \
    --rootfs build/out/rootfs.ext4 \
    --boot-dir <boot tree dir> --config-size 256M
```

`images/t2-image.py --help` lists every option; `--size auto` produces the
smallest image that holds the payloads.  The output is sparse, so it stores
cheaply and flashes fast with `dd conv=sparse`.
