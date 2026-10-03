# Kernel

Mainline Linux for the ZSpace T2 (RK3568), built from tag **v7.3-rc5** plus five
patches. The patches and their rationale are documented in
[`patches/README.md`](patches/README.md).

## Patch set

| Patch | Purpose |
|---|---|
| `0001-pci-dw-rockchip-power-cycle-endpoint-on-link-retry` | power-cycle the PCIe endpoint and retry link training (AP6275P WiFi quirk) |
| `0002-arm64-dts-rockchip-add-rk3568-t2` | the `rk3568-t2` board device tree and its `Makefile` entry |
| `0003-media-dt-bindings-rockchip-add-rk3568-video-decoder` | device-tree bindings for the RK3568 VDPU346 video decoder |
| `0004-media-rkvdec-add-support-for-the-vdpu346-variant` | `rkvdec` driver support for the VDPU346 variant (HEVC) |
| `0005-arm64-dts-rockchip-add-the-vdpu346-video-decoders-on-rk356x` | enable the VDPU346 decoders in `rk356x-base.dtsi` |

## Build

Requires `git`, GNU make and the `aarch64-linux-gnu-` cross toolchain.

Firmware and the initramfs come before the kernel, because the shipped `Image`
embeds the initramfs tree (and that tree carries the WiFi firmware the module
needs at early probe). The full chain is:

**firmware fetch → initramfs → kernel → u-boot → rootfs → installer image**

```sh
rootfs/fetch.sh              # vendor WiFi/BT firmware
rootfs/initramfs/build.sh    # -> build/initramfs
kernel/fetch.sh              # clone mainline v7.3-rc5 into build/kernel
kernel/build.sh              # apply patches, configure, build, install
```

`kernel/build.sh` applies the patches with `git am`, copies
`kernel/config/kernel.config` over `build/kernel/.config`, rewrites
`CONFIG_INITRAMFS_SOURCE` to this clone's `build/initramfs`, runs
`make olddefconfig`, then builds `Image`, `dtbs` and `modules` with
`-j$(nproc)`. Finished artefacts go to `build/out/`:

* `Image` — the arm64 kernel image (with the initramfs embedded)
* `rk3568-t2.dtb` — the board device tree
* `modules/lib/modules/…` — the module tree

The script stops with a clear error when `build/initramfs` is missing, rather
than embedding a stale or empty tree. It also refuses to run when `build/kernel`
is missing or already carries the patches; delete that tree and re-run
`kernel/fetch.sh` to start clean.

`ARCH` (default `arm64`), `CROSS_COMPILE` (default `aarch64-linux-gnu-`) and
`JOBS` can be overridden in the environment.

The kernels in the shipped images were built with this patch set and the config
below. A full kernel build takes well over an hour, so it is **not** part of the
repository smoke test — the fetch and patch-application path is what is checked.

## Kernel config

`kernel/config/kernel.config` is the exact `.config` the shipped images were
built with. It is committed here because `build/kernel/.config` is git-ignored in
the kernel tree and therefore cannot travel in a patch.

The file is committed verbatim, with one exception: the absolute
`CONFIG_INITRAMFS_SOURCE` path it was built with is rewritten at build time by
`kernel/build.sh` to this clone's `build/initramfs`. No other symbol is touched.

It is derived in three stages:

1. `make ARCH=arm64 defconfig` — the arm64 multiplatform defconfig.
2. **Bootstrap** — enable the board's drivers (`ARCH_ROCKCHIP`,
   `PCIE_ROCKCHIP_DW_HOST`, NVMe, USB XHCI/dwc3/configfs gadget, the Rockchip
   thermal/sensor blocks, `DRM_ROCKCHIP`, `CFG80211`/`MAC80211`/`BRCMFMAC`,
   `R8169`, …) and set a few media helpers to `=m`.
3. **Trim** — turn off silicon this board does not have: the other arm64
   platforms and their clock/pinctrl subtrees, ACPI, KVM/Xen/virtio, the SCSI
   HBA and ATA families, TV/DVB media, other-SoC USB controllers, `KALLSYMS_ALL`
   and whole classes of unattachable modules (other-vendor GPUs, NICs and
   wireless). Load-bearing drivers are asserted to survive.

### Regenerating the config

The pipeline lives in `lib/t2-build.py`, against a kernel tree you have already
fetched. It resolves the repository from its own location:

* `--bootstrap-config` (with the kernel step) — on a fresh checkout, runs
  `defconfig` and enables the curated bootstrap set; only for a tree with no
  `.config`.
* `lib/t2-build.py --trim-config` — applies the trim lists to the tree's
  `.config` and runs `olddefconfig`, then asserts the result. It is idempotent,
  so re-running it never drifts.
* `lib/t2-build.py --check-config` — asserts the curated lists against the real
  `.config` (every load-bearing symbol `=y`, every trimmed symbol off); this
  guards against a rebuild silently losing a driver the Image needs.

The two symbol lists (`BOOTSTRAP_ENABLE` and the `TRIM_*` sets) inside that
module are the whole policy; editing them is the only intended way to change the
config. To adopt a regenerated config here, copy the resulting `.config` over
`kernel/config/kernel.config`.
