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

All five apply cleanly to pristine v7.3-rc5 in that order. The blobs the
patches produce for every path they touch are:

| path | blob |
|---|---|
| `drivers/pci/controller/dwc/pcie-dw-rockchip.c` | `272e4c7bc048` |
| `arch/arm64/boot/dts/rockchip/rk3568-t2.dts` | `105e28056585` |
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
blobs are not redistributable; they are committed in `rootfs/firmware/brcm/`, and
`rootfs/fetch.sh` verifies them and can refresh them from a running T2 or a
vendor update. Also enable
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
`pci_host_probe()` path, with no rescan.  The measurements behind every choice
above (the link training times, the 500 ms and 1 s waits, and the 5.9 s → 2.9 s
probe saving) are in the patch's own commit message.

## Measured, not adopted

**`probe_type = PROBE_PREFER_ASYNCHRONOUS` on `rockchip_pcie_driver`.**  The PCIe
probes cost this board ~2.8 s inside the kernel phase, so probing them
asynchronously looks like free boot time.  It is not.

The change does what it says: over three boots the kernel phase drops from a
stable 4.76-4.80 s to 3.99-4.08 s, the NVMe links train at ~1.1 s, and the WiFi
link still comes up at ~3.6 s with brcmfmac loading and associating normally,
with no probe errors.  But the PCIe functions then appear *while*
`systemd-udev-trigger` is running, and NetworkManager waits for udev's initial
enumeration before it starts, so it is held back: it runs at 5.8-6.1 s instead
of 4.2-4.4 s, and the userspace phase goes from 7.54-8.54 s (median 7.60 s,
synchronous) to a steady 8.36-8.54 s (median 8.50 s, asynchronous).

Kernel + userspace totals are 12.30-13.32 s (median 12.41 s) synchronous against
12.39-12.58 s (median 12.54 s) asynchronous, and the serial banner-to-login time
is 18-19 s either way.  The 0.75 s the kernel saves is spent again in udev, so
this is not in the patch set.  (Three boots per variant, `systemd-analyze`
`kernel`/`userspace` plus the serial log's banner-to-login delta.)
