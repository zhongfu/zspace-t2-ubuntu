# U-Boot for the ZSpace T2 (RK3568)

Mainline U-Boot v2026.07 for the board, with the T2's own device tree and a
small board configuration. The build produces two images: one for the eMMC, one
for the installer card.

## What the board port adds

* **Control device tree.** `dts/rk3568-t2.dts` is the board tree
  (`compatible = "zspace,t2"`, `rockchip,rk3568`): RK809 regulators, the UART2
  console, eMMC and SD, the front-panel LEDs and the SARADC `adc-keys`.
  `dts/rk3568-t2-u-boot.dtsi` includes `rk3568-u-boot.dtsi` explicitly and
  deletes the LED `default-state` properties.
* **Power-on gate and LEDs.** `CONFIG_PREBOOT` reads the RK809 power-on source
  (register `0xf5`, i2c0 at `0x20`) and the SoC reset cause. It powers the board
  back off on a fresh plug-in power-on; a button, reset, or watchdog start boots
  normally and lights the red LED. Linux later takes over the same `gpio-leds`
  (green on, red off). Env `t2_boot_on_plugin=1` overrides the gate.
* **Environment on the boot FAT.** `CONFIG_ENV_IS_IN_FAT` with
  `CONFIG_ENV_FAT_DEVICE_AND_PART=":3"` keeps `/uboot.env` in partition 3, the
  FAT boot partition, from the eMMC or the SD card.
* **Bootcount A/B fallback.** `CONFIG_BOOTCOUNT_LIMIT` and
  `CONFIG_BOOTCOUNT_ALTBOOTCMD` boot `/Image.old` after three failed boots.
* **Two boot commands.** The plain image boots the eMMC boot tree; holding the
  power button about 0.5 s selects the card. The installer image boots the
  card's installer tree. Both keep the `rockusb` flash workflow over the Type-C
  OTG port.
* **Host patch.** `patches/0001-dtc-pylibfdt-swig4.patch` teaches
  `scripts/dtc/pylibfdt/Makefile` the Python 2 compatibility defines that swig 4
  no longer emits (`PyInt_AsLong`, `PyString_*`).

## Build

You need a Linux x86-64 host, `git`, `python3`, `swig` (4 or newer), `bison`,
`flex`, and an aarch64 cross compiler (`gcc-aarch64-linux-gnu`, or
`CROSS_COMPILE` set).

```sh
u-boot/fetch.sh     # build/uboot (U-Boot v2026.07) + build/rkbin (bin/rk35)
u-boot/build.sh     # -> build/out/
```

`fetch.sh` clones U-Boot at tag `v2026.07` and a sparse
`rockchip-linux/rkbin` (only `bin/rk35`). `build.sh` installs the defconfigs and
device trees, applies the patches, and builds the installer image first, then
the plain image. `SOURCE_DATE_EPOCH` is pinned to the U-Boot commit's time.

The rkbin blobs are `bin/rk35/rk3568_bl31_v1.46.elf` (BL31) and
`bin/rk35/rk3568_ddr_1560MHz_v1.26.bin` (TPL). Override them with `BL31` /
`ROCKCHIP_TPL`; override `UBOOT`, `RKBIN`, `OUT`, or `JOBS` to change the trees,
output, or job count.

## Output

Everything is written to `build/out/`:

| File | Purpose |
|---|---|
| `u-boot.itb` | U-Boot + BL31 + DT; the eMMC's raw loader, LBA `0x4000` (GPT p1 `uboot`) |
| `idbloader.img` | TPL + SPL; the eMMC's raw loader, LBA `0x40` |
| `u-boot-installer.itb` | the installer image for the card's loader partition |
| `idbloader-installer.img` | the installer's TPL + SPL |
| `u-boot-initial-env`, `u-boot-installer-initial-env` | the compiled default environment as text |

`u-boot.itb` carries the LED strings (`power-led-red`, `power-led-green`) and
the gate/environment logic. `u-boot-initial-env` is the text other tools turn
into `/uboot.env`.

## Patch verification

`patches/0001-dtc-pylibfdt-swig4.patch` applies cleanly to upstream U-Boot tag
`v2026.07` (commit `ece349ade2973e220f524ce59e59711cc919263f`) with
`git apply --check`, no offset or reject. Re-check with:

```sh
git -C build/uboot checkout -- .          # restore a clean v2026.07 tree
git -C build/uboot apply --check u-boot/patches/0001-dtc-pylibfdt-swig4.patch
```
