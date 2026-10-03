# Kernel

Mainline Linux for the ZSpace T2 (RK3568), built from tag **v7.3-rc5** plus five
patches. [`patches/README.md`](patches/README.md) documents them.

## Patch set

| Patch | Purpose |
|---|---|
| `0001-pci-dw-rockchip-power-cycle-endpoint-on-link-retry` | power-cycle the PCIe endpoint and retry link training (AP6275P WiFi quirk) |
| `0002-arm64-dts-rockchip-add-rk3568-t2` | the `rk3568-t2` board device tree and its `Makefile` entry |
| `0003-media-dt-bindings-rockchip-add-rk3568-video-decoder` | device-tree bindings for the RK3568 VDPU346 video decoder |
| `0004-media-rkvdec-add-support-for-the-vdpu346-variant` | `rkvdec` driver support for the VDPU346 variant (HEVC) |
| `0005-arm64-dts-rockchip-add-the-vdpu346-video-decoders-on-rk356x` | enable the VDPU346 decoders in `rk356x-base.dtsi` |

## Build

You need `git`, GNU make, and the `aarch64-linux-gnu-` cross toolchain. The
shipped `Image` embeds the initramfs tree, and that tree carries the WiFi
firmware the module needs at early probe, so build in this order:

**firmware fetch → initramfs → kernel → u-boot → rootfs → installer image**

```sh
rootfs/fetch.sh              # vendor WiFi/BT firmware
rootfs/initramfs/build.sh    # -> build/initramfs
kernel/fetch.sh              # clone mainline v7.3-rc5 into build/kernel
kernel/build.sh              # apply patches, configure, build, install
```

`kernel/build.sh` applies the patches with `git am`. It copies
`kernel/config/kernel.config` over `build/kernel/.config`, rewrites
`CONFIG_INITRAMFS_SOURCE` to this clone's `build/initramfs`, runs
`make olddefconfig`, and builds `Image`, `dtbs`, and `modules` with
`-j$(nproc)`. Finished artefacts go to `build/out/`:

* `Image` — the arm64 kernel image, with the initramfs embedded
* `rk3568-t2.dtb` — the board device tree
* `modules/lib/modules/…` — the module tree

The script stops if `build/initramfs` is missing, and refuses to run if
`build/kernel` is missing or already carries the patches. To start clean, delete
that tree and re-run `kernel/fetch.sh`.

`ARCH` (default `arm64`), `CROSS_COMPILE` (default `aarch64-linux-gnu-`), and
`JOBS` can be overridden in the environment.

## Kernel config

`kernel/config/kernel.config` is the exact `.config` the shipped images were
built with, committed because `build/kernel/.config` is git-ignored.
`kernel/build.sh` rewrites its absolute `CONFIG_INITRAMFS_SOURCE` path to this
clone's `build/initramfs`; no other symbol is touched.

The config comes from three stages:

1. `make ARCH=arm64 defconfig`.
2. **Bootstrap** — enable the board's drivers: `ARCH_ROCKCHIP`,
   `PCIE_ROCKCHIP_DW_HOST`, NVMe, USB XHCI/dwc3/configfs gadget, the Rockchip
   thermal/sensor blocks, `DRM_ROCKCHIP`, `CFG80211`/`MAC80211`/`BRCMFMAC`, and
   `R8169`. Set a few media helpers to `=m`.
3. **Trim** — turn off silicon this board does not have. This includes other
   arm64 platforms, ACPI, KVM/Xen/virtio, the SCSI HBA and ATA families, TV/DVB
   media, other-SoC USB controllers, and `KALLSYMS_ALL`. It also drops
   unattachable module classes: other-vendor GPUs, NICs, and wireless.

### Regenerating the config

The pipeline is `lib/t2-build.py`, run against a fetched kernel tree.

* `--bootstrap-config` — runs `defconfig` and enables the curated bootstrap set;
  only for a tree with no `.config`.
* `--trim-config` — applies the trim lists and `olddefconfig`, then asserts the
  result. Idempotent.
* `--check-config` — asserts the curated lists against the real `.config`.

The two symbol lists (`BOOTSTRAP_ENABLE` and the `TRIM_*` sets) are the whole
policy. To adopt a regenerated config, copy the resulting `.config` over
`kernel/config/kernel.config`.
