# Vendor firmware

The AP6275P WiFi/BT module on the ZSpace T2 (Broadcom/Cypress BCM43752) needs
firmware that is **not redistributable**: it is not in `linux-firmware`, only
Rockchip and the module vendor ship it.  This directory is therefore empty in
the repository; `rootfs/fetch.sh` fills it from a source that already has it.

Everything else the images need is either committed
(`rootfs/initramfs/firmware/`: the Realtek RTL8156B NIC blobs and the wireless
regulatory database) or built from source (BusyBox).

## Files

`rootfs/fetch.sh` copies these four files into `rootfs/firmware/brcm/` and
verifies their SHA-256.  All four come from the vendor rootfs directory
`/system/etc/firmware/` (Rockchip's `bcmdhd` firmware directory; on the vendor
image `/vendor/etc/firmware` is a symlink to it).

| File | Size (B) | SHA-256 | Provenance |
|---|---|---|---|
| `fw_bcm43752a2_pcie_ag.bin` | 936074 | `6a2dbe01e72221defba91a52e158768d973a3c85ca2d881c924379e35ad36b23` | vendor `/system/etc/firmware/fw_bcm43752a2_pcie_ag.bin`; the WiFi firmware binary (mandatory for brcmfmac) |
| `clm_bcm43752a2_ag.blob` | 29225 | `5143146e1923f87f7aab8df043abcf89a657fa9fdc3b22a38806399730d9a97a` | vendor `/system/etc/firmware/clm_bcm43752a2_ag.blob`; the regulatory CLM blob (optional for brcmfmac) |
| `nvram_AP6275P.txt` | 7458 | `b4780f7b86a5680dbe496815dbe07aa22010701de2d8c60570b8274a42db215b` | vendor `/system/etc/firmware/nvram_AP6275P.txt`; the board NVRAM |
| `BCM4362A2.hcd` | 80602 | `18901a5bef1d418b6895e92d0afae36234f4160b237465dfca3d75e9844e93ef` | vendor `/system/etc/firmware/BCM4362A2.hcd`; the Bluetooth firmware patch.  The board's controller (BCM43752) reports LMP subversion `0x1111`, which the driver maps to `BCM4362A2`, so this is the `.hcd` the board actually loads - the vendor ships eleven `BCM*.hcd` files but only this one is needed |

The table is the authority for what `rootfs/fetch.sh` verifies; update it if a
new vendor package ships different blobs.

`rootfs/fetch.sh` also creates driver-named copies of the WiFi files, because
mainline `brcmfmac` asks for:

| brcmfmac name | copied from |
|---|---|
| `brcmfmac43752-pcie.bin` | `fw_bcm43752a2_pcie_ag.bin` |
| `brcmfmac43752-pcie.txt` | `nvram_AP6275P.txt` |
| `brcmfmac43752-pcie.clm_blob` | `clm_bcm43752a2_ag.blob` |

`rootfs/build.sh` stages `rootfs/firmware/brcm/` plus the committed
`rootfs/initramfs/firmware/` into `build/firmware/`, which the profile's
`image.json` names as its `firmware_src`; hooks/50-firmware.sh copies from
there into the image.  `rootfs/initramfs/build.sh` copies the same blobs into
the kernel's embedded initramfs for the early brcmfmac probe.

## Sources

`rootfs/fetch.sh` obtains the files from any one of:

* `--from-host root@HOST` - a T2 that still runs the vendor firmware, over
  SSH (`/system/etc/firmware/`).
* `--ota URL` - a vendor `.zspace` update/OTA package over HTTP(S), read with
  the repository's OTA reader without downloading the whole package.
* `--from-dir DIR` - a directory that already holds the files (a mounted
  vendor rootfs, or an OTA package extracted by hand).

Run it before the initramfs and rootfs builds:

```sh
rootfs/fetch.sh --from-host root@192.168.1.50
rootfs/fetch.sh --ota https://example.invalid/t2/update.zspace
```
