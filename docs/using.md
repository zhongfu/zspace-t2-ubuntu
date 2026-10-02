# Using the device

This page installs the image, provisions the board on first boot, and backs up
the eMMC.  Build the images first: see `docs/building.md`.

## Install from the SD installer image

Write the installer image to an SD card.  Identify the card carefully; `dd`
overwrites the whole device.

```sh
lsblk -o NAME,SIZE,MODEL     # find the card, e.g. /dev/sdb
sudo dd if=build/out/installer.img of=/dev/sdb bs=4M status=progress conv=fsync
sync
```

Then:

1. Insert the card into the SD slot.
2. Power the board on while you **hold the power button**.  U-Boot samples the
   power button twice, 0.4 s apart, and boots the card's installer tree when it
   is held; otherwise it boots the eMMC.  U-Boot lights the red power LED when
   it selects the installer.
3. The installer card's `T2-CONFIG` partition carries `flash.presses=` and
   `flash.window=`.  The shipped example uses **10 presses within 60 s**.  Press
   the power button that many times inside the window.

The red power LED shows the count: it blinks 1.0 s on / 1.0 s off before the
first press, then 0.1 s shorter after every counted press (press 1 -> 0.9 s,
press 9 -> 0.1 s).  At the target count the red LED goes solid for 5 s, then the
write phase alternates red and green at about 2 Hz until green is steady.

The installer repartitions the eMMC, formats the boot partition, writes the
rootfs, and writes the loader.  **Do not remove power during the write.**  If
you miss the window, or press the wrong number of times, the installer writes
nothing and drops to its recovery shell on the serial console, telnet, and the
USB gadget.

> A one-partition card variant uses `install.presses=` and `install.window=` and
> writes a single partition instead.  The image tools no longer build that card,
> but the installer still supports the keys.

## First boot and provisioning

On first boot the system provisions itself from a small FAT partition labelled
**`T2-CONFIG`** (uppercase) that holds `/t2-config.txt`.  `t2-provision.service`
mounts it read-only, applies the keys, and stamps the file's sha256, so an
unchanged file is a no-op on later boots while an edited file re-applies.

| Key | Effect |
|---|---|
| `hostname=` | sets the board's hostname |
| `wifi.ssid=` / `wifi.psk=` | writes a NetworkManager WiFi connection |
| `wifi.country=` | sets the WiFi regulatory country (needs `iw`) |
| `ssh.authorized_key=` | appended to `/root/.ssh/authorized_keys`; repeatable |
| `ble.psk=` | sets the Bluetooth LE pre-shared key |

Unknown keys are logged and ignored.  `install.*` and `flash.*` keys are not
handled on a normal boot; only the installer reads them.

Default access:

* Log in as **`root`** with password **`t2`**, then change it with `passwd`.
* The serial console is `ttyS2` at 1500000 8N1.  The HDMI console is `tty1`.
* SSH accepts the same password; no public key is baked into the image.

**USB-C gadget link.**  `t2-usbgadget.service` presents one CDC-NCM network
function on the Type-C OTG port.  The board is `10.55.55.2/24` and serves DHCP
to the laptop.  When the `T2-CONFIG` partition exists and is not mounted, the
service also exports it as a removable read-write FAT drive, so the laptop can
edit `t2-config.txt`.  On the host, run:

```sh
tools/t2-gadget-link.py          # activate a NetworkManager profile, print the address
ssh root@10.55.55.2              # or ssh root@t2.local (avahi)
```

**Bluetooth LE.**  `t2-ble.service` runs a GATT management channel with no
cable.  The advertisement carries the hostname.  Pair first (LESC "Just Works"),
then the protocol authenticates each message with an HMAC over a pre-shared key.
The key comes from `ble.psk=` in the config file; otherwise the service
generates one on first start and logs it.  `t2-ble-password` prints the key in
force (root only).  From a host:

```sh
bluetoothctl scan le
bluetoothctl connect <board-address>
bluetoothctl pair    <board-address>
```

The reference client is `t2-ble.html`, a single-file Web Bluetooth page.  It
needs a secure context (HTTPS or `localhost`) and desktop Chrome or Edge.

## Back up the eMMC

Two routes.  Route (a) runs on the vendor OS; route (b) needs no OS at all.

### (a) From the vendor ZOS, over SSH

The vendor OS runs a Debian-based ZOS.  Log in as root over the serial console
(the vendor OS auto-logs in root on `ttyFIQ0`), or over SSH if root SSH is
enabled.

```sh
ssh root@<nas> 'cat /proc/partitions'
ssh root@<nas> 'dd if=/dev/mmcblk0p1 bs=4M' > p1-uboot.img
ssh root@<nas> 'dd if=/dev/mmcblk0p3 bs=4M' > p3-boot.img
ssh root@<nas> 'sha256sum /dev/mmcblk0p3'
sha256sum p3-boot.img
```

Keep at least the bootchain partitions: `p1` (`uboot`), `p2` (`misc`), `p3`
(`boot`), `p6` (`rootfs`), `p10` (recovery kernel), and `p11` (recovery rootfs).
`p8` is user data.  A whole-device dump is slower but simplest:

```sh
ssh root@<nas> 'dd if=/dev/mmcblk0 bs=4M | gzip -1' > emmc.img.gz
```

Do not read or write `/dev/mmcblk0rpmb`; it is authenticated and cannot be
restored.  Check `/dev/mmcblk0boot0` and `mmcblk0boot1` too, in case the loader
lives in an eMMC boot partition.  The repository's `tools/zspace-fetch.sh`
automates this and verifies each partition with a device-side hash.

Remember: the vendor USB recovery stick writes only `p3/p6/p8/p10/p11` and
**never** the loader.  A bad loader write needs the maskrom route.

### (b) Interrupt U-Boot, then use `rkdeveloptool`

This path needs only the serial console.

1. Reset the board: send a serial BREAK, then `b` (SysRq-b); or power-cycle it.
2. Send CTRL+C during U-Boot's autoboot countdown to reach the `=>` prompt -
   the window is one second (`CONFIG_BOOTDELAY=1`; the board ships that value
   because the countdown is dead time on every boot).
3. Put the eMMC behind `rkdeveloptool`:

```sh
rockusb 0 mmc 0          # at the U-Boot prompt
```

Then, on the host:

```sh
rkdeveloptool ld                     # expect: Loader
rkdeveloptool rfi                    # flash capacity
rkdeveloptool ppt                    # print the partition table
rkdeveloptool rl 0x8000 0x20000 boot.img   # read 64 MiB, sector-addressed
rkdeveloptool rd                     # reset the board
```

Known quirk: `rl` returns `0xcc` for any LBA at or above `0x10000` (32 MiB),
through both maskrom and U-Boot rockusb.  This is a read-path quirk, not a
device limit.  Writes are unaffected.  Write in chunks of at most 8 MiB at
explicit LBAs, and read-verify only below 32 MiB; treat the boot as the proof
for the rest.  `tools/t2-flash.py` drives this workflow, including the reset and
the CTRL+C catch.

## What is not covered

The vendor's proprietary applications (`zfilev2`, `zalbumv2`, `znvr`, and
others), and the NPU and ISP userspace, are not part of this build.  The GPU
uses ARM's `kbase` driver in the vendor stack; a mainline kernel would need a
different userspace.  See the "What works" table in the repository root
`README.md` for the supported features.
