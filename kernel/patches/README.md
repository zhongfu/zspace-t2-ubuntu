# ZSpace T2 kernel patches

Everything needed on top of a **mainline kernel tree** to run on the ZSpace T2
(RK3568). **Five patches**: the single PCIe quirk this board turned out to
require, the board DTS, and the three VDPU346 rkvdec commits kept locally by
user decision (VDPU346 HEVC decode has no upstream driver yet).

Base: **v7.3-rc5** (`72d3fcf802c4`), the commit all five apply to in order.
`kernel/fetch.sh` clones that tag into `build/kernel`.

| # | patch | what it does |
|---|---|---|
| `0001-pci-dw-rockchip-power-cycle-endpoint-on-link-retry.patch` | lets `rockchip-dw-pcie` power-cycle the endpoint and retry when the link does not train — the AP6275P WiFi quirk |
| `0002-arm64-dts-rockchip-add-rk3568-t2.patch` | the board DTS (file-creating, 1228 lines) plus its `Makefile` entry |
| `0003-media-dt-bindings-rockchip-add-rk3568-video-decoder.patch` | DT bindings for the RK3568 VDPU346 video decoder |
| `0004-media-rkvdec-add-support-for-the-vdpu346-variant.patch` | `rkvdec` support for the VDPU346 variant (HEVC) |
| `0005-arm64-dts-rockchip-add-the-vdpu346-video-decoders-on-rk356x.patch` | enables the VDPU346 decoders in `rk356x-base.dtsi` |

## Reconstructing the tree

```sh
kernel/fetch.sh                  # -> build/kernel, tag v7.3-rc5
kernel/build.sh                  # git am, config, Image + dtbs + modules
```

`kernel/build.sh` applies the five patches to `build/kernel` with
`git am`, copies `kernel/config/kernel.config` over `build/kernel/.config`, runs
`make olddefconfig`, and builds `Image`, `dtbs` and `modules`. The finished
`Image`, `rk3568-t2.dtb` and module tree land in `build/out/`.

The equivalent manual recipe:

```sh
cd build/kernel
git am ../../kernel/patches/*.patch    # the glob expands in numeric order, 0001 -> 0005
```

All five apply cleanly to pristine v7.3-rc5 in that order. The result matches the
live build tree (`workbench/mainline-7.3`) byte-for-byte for every path the
patches touch:

| path | blob |
|---|---|
| `drivers/pci/controller/dwc/pcie-dw-rockchip.c` | `8eacc9ce8c28` |
| `arch/arm64/boot/dts/rockchip/rk3568-t2.dts` | `fd6ad60bf0f0` |
| `arch/arm64/boot/dts/rockchip/Makefile` | `bd77bad158f8` |
| `drivers/media/platform/rockchip/rkvdec/rkvdec.c` | `4647120067f7` |
| `arch/arm64/boot/dts/rockchip/rk356x-base.dtsi` | `eaf9dc27f6a6` |
| `Documentation/devicetree/bindings/media/rockchip,vdec.yaml` | `ea5386e84f17` |

## What is *not* in the patches

The patches are the entire kernel **source** delta. Two build inputs deliberately
stay outside them, because they are not kernel source (or are gitignored) and so
cannot be `git am`'d:

* **`.config`** — gitignored inside the kernel tree, so it is not a patch. It is
  committed here as `kernel/config/kernel.config` and copied into the fresh tree
  by `kernel/build.sh`. It is what sets `CONFIG_BRCMFMAC_PCIE=y` and embeds the
  initramfs.
* **the bring-up initramfs source tree** referenced by `CONFIG_INITRAMFS_SOURCE`
  (provided by `rootfs/initramfs/`), plus the WiFi firmware blobs below.

## Firmware

`/lib/firmware/brcm/` needs the AP6275P's **PCIe** firmware. Because the module
can enumerate before the root filesystem is switched in, put it in the
**initramfs** as well if you use one — otherwise the firmware load fails with
`-ENOENT` when the device is probed early:

| file | copy of the vendor blob |
|---|---|
| `brcmfmac43752-pcie.bin` | `fw_bcm43752a2_pcie_ag.bin` |
| `brcmfmac43752-pcie.txt` | `nvram_AP6275P.txt` |
| `brcmfmac43752-pcie.clm_blob` | `clm_bcm43752a2_ag.blob` |

Only the `.bin` is mandatory (`txcap_blob` is optional and reported missing). The
blobs are not redistributable and are not in this repository; `rootfs/fetch.sh`
collects them from a running T2 or a vendor update. Also enable
`CONFIG_BRCMFMAC_PCIE=y` — mainline's `BRCMFMAC_SDIO` alone will not bind this
part, which is a PCIe device (`14e4:449d`), not SDIO.

## Why patch 0001 exists

Mainline has no way to express this. The BCM43752 only trains its link once it
has been **power-cycled with the PHY running and PERST# already released**, which
is later than `dw_pcie_wait_for_link()` waits — and the mechanism that would
otherwise enumerate such a late link-up (`0e0b45ab5d77`, Link Up IRQ →
`pci_rescan_bus()`) was **reverted upstream**. `pwrctrl` is merged but powers the
endpoint at the *start* of probe, i.e. the opposite order, and
`rockchip,perst-inactive-ms` is BSP-only. Patch 0001 therefore keeps a handle on
the endpoint's `vpcie3v3` supply and cycles it on failure, mirroring the
in-flight "[RFC PATCH] PCI: rockchip-host: Retry link training on failure without
PERST#" series.

No userspace helper is needed: the device is enumerated on the normal
`pci_host_probe()` path, with no rescan. See entry 18 of
`notes/mainline-7.3-build.md` in the bring-up workspace for the measurements
behind every choice above.
