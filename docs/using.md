# Using the device

Install the image, provision the board, and back up the eMMC. Build first: see
`docs/building.md`.

## What the lights mean

Normal operation:

| Light | Meaning |
|---|---|
| power, green | steady on: the system is running |
| power, red | fault. This image leaves it off |
| drive bay green (each M.2 bay) | steady: a drive is present. Blinking: the drive is active. Off: the bay is empty |
| drive bay red (each M.2 bay) | steady: the drive is faulty, or its md array is degraded |

## Install from the SD installer image

Write the installer image to an SD card. `dd` overwrites the whole device, so
identify the card.

```sh
lsblk -o NAME,SIZE,MODEL     # find the card, e.g. /dev/sdb
sudo dd if=build/out/installer.img of=/dev/sdb bs=4M status=progress conv=fsync
sync
```

Then:

1. Insert the card.
2. Plug in the power supply. A plug-in power-on is not an autoboot: U-Boot
   shuts the board back off and nothing lights up. The console prints
   `T2 power-on: pmic=0x40+0x0 reset-cause=0x0` and
   `T2 powered on by plug-in - powering off`; wait a second for it to go off.
3. Press and hold the power button. The board autoboots only on a button press.
   About a second later red comes on steady: U-Boot has read the PMIC power-on
   source. U-Boot never blinks; every LED state is steady.
4. Keep holding through the 1 s countdown. U-Boot then selects the card's
   installer tree (`T2: installer: booting the SD card`) and leaves red lit. A
   tap, or no press, boots the eMMC and switches the LED to green. Holding on
   is harmless.
5. The kernel briefly lights green; the installer then takes over. Red means
   "installer running".
6. Press the card's `flash.presses=` count within `flash.window=`. The shipped
   example uses **10 presses within 60 s**.

If a board lights green before that red and shows a 2-second autoboot countdown,
it uses an old saved U-Boot environment. Reflash the board, or run
`env default -a; saveenv` at the U-Boot console. The installer keeps the
environment it finds as `/uboot.env.stock` on the boot partition, and writes its
own as `/uboot.env`.

The red LED shows the count. Before the first press it flashes 1.0 s on, 1.0 s
off. After each counted press the on time is 0.1 s shorter (press 1 -> 0.9 s,
press 9 -> 0.1 s). At the target count it goes solid for 5 s, then stays solid
while the payload is verified.

Every sector written alternates red and green at about 2 Hz. **Do not cut
power while it alternates.** Green is steady once the last sync returns.

If a run does not finish, the red LED blinks **two short flashes, then a
pause**, repeating. It then opens its recovery shell on the serial console,
telnet, and the USB gadget. This happens when the card was not armed,
the count was missed, the payload was refused, or a write failed. A dry run ends
the same way.

The installer repartitions the eMMC, formats the boot partition, and writes the
rootfs and loader. If you miss the window or press the wrong number of times, it
writes nothing.

> A one-partition card variant uses `install.presses=` and `install.window=`;
> the installer still supports the keys.

## First boot and provisioning

On first boot the system provisions itself from a FAT partition labelled
**`T2-CONFIG`** (uppercase), which holds `/t2-config.txt`;
`t2-provision.service` applies the keys when the file changes.

| Key | Effect |
|---|---|
| `hostname=` | sets the board's hostname |
| `wifi.ssid=` / `wifi.psk=` | writes a NetworkManager WiFi connection |
| `wifi.country=` | sets the WiFi regulatory country (needs `iw`) |
| `ssh.authorized_key=` | appended to `/root/.ssh/authorized_keys`; repeatable |
| `ble.psk=` | sets the Bluetooth LE pre-shared key |

Unknown keys are ignored; `install.*` and `flash.*` are only read by the
installer.

Log in as **`root`** with password **`t2`**, then run `passwd`. The serial
console is `ttyS2` at 1500000 8N1; HDMI is `tty1`. SSH uses the same password
and ships no public key.

**USB-C gadget link.** `t2-usbgadget.service` gives a USB network link at
`10.55.55.2/24` with DHCP, and exports `T2-CONFIG` for editing `t2-config.txt`.
On the host:

```sh
tools/t2-gadget-link.py          # print the board's address
ssh root@10.55.55.2              # or ssh root@t2.local (avahi)
```

**Bluetooth LE.** `t2-ble.service` gives cable-free management; pair first, then
messages use the key from `ble.psk=` (or one generated and logged on first
start). `t2-ble-password` prints it (root only). From a host:

```sh
bluetoothctl scan le
bluetoothctl connect <board-address>
bluetoothctl pair    <board-address>
```

The reference client, `t2-ble.html`, is a single-file Web Bluetooth page needing
HTTPS or `localhost` and Chrome or Edge.

## Powering off

The power button is a hold: a short press does nothing, and a hold of 3 s
(`press_seconds` in `/etc/t2/powerkey.conf`) shuts down gracefully. The red
power LED lights while you hold, and blinks while the shutdown runs.

## Back up the eMMC

Two routes: (a) on the vendor OS, (b) with no OS.

### (a) From the vendor ZOS, over SSH

```sh
ssh root@<nas> 'cat /proc/partitions'
ssh root@<nas> 'dd if=/dev/mmcblk0p1 bs=4M' > p1-uboot.img
ssh root@<nas> 'dd if=/dev/mmcblk0p3 bs=4M' > p3-boot.img
ssh root@<nas> 'sha256sum /dev/mmcblk0p3'
sha256sum p3-boot.img
```

Keep at least the bootchain partitions: `p1` (`uboot`), `p2` (`misc`), `p3`
(`boot`), `p6` (`rootfs`), `p10` (recovery kernel), `p11` (recovery rootfs);
`p8` is user data. A whole-device dump is simpler:

```sh
ssh root@<nas> 'dd if=/dev/mmcblk0 bs=4M | gzip -1' > emmc.img.gz
```

Do not touch `/dev/mmcblk0rpmb`; it cannot be restored. Check
`/dev/mmcblk0boot0` and `mmcblk0boot1` too, in case the loader lives there.
`tools/zspace-fetch.sh` automates this.

The vendor USB recovery stick writes only `p3`, `p6`, `p8`, `p10`, and `p11`,
never the loader. A bad loader write needs maskrom.

### (b) Interrupt U-Boot, then use `rkdeveloptool`

1. Reset the board: send a serial BREAK, then `b` (SysRq-b); or power-cycle it.
2. Send CTRL+C during the autoboot countdown to reach the `=>` prompt. The
   window is one second.
3. Put the eMMC behind `rkdeveloptool`:

```sh
rockusb 0 mmc 0          # at the U-Boot prompt
```

Then on the host:

```sh
rkdeveloptool ld                     # expect: Loader
rkdeveloptool rfi                    # flash capacity
rkdeveloptool ppt                    # print the partition table
rkdeveloptool rl 0x8000 0x20000 boot.img   # read 64 MiB, sector-addressed
rkdeveloptool rd                     # reset the board
```

Known quirk: `rl` fails for any LBA at or above `0x10000` (32 MiB). Write in
chunks of at most 8 MiB at explicit LBAs, and read-verify only below 32 MiB.
`tools/t2-flash.py` drives this workflow.

## When the root filesystem does not come up

If `root=LABEL=zspace-rootfs` cannot be resolved or mounted, the initramfs drops
to its bring-up shell. It reports the root spec, what it resolved to, the
reason, and each device's signature. Add `t2.debug=1` to the kernel
cmdline, or `T2_INIT_DEBUG=1` to the environment, for the full dump.
