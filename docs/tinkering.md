# Tinkering with the ZSpace T2

This page covers the physical side of the board: opening the case, the serial
console, the maskrom button, and SD boot. Build the images with
`docs/building.md`. Install and use them with `docs/using.md`.

## Opening the case

1. Power the board off and unplug the power supply.
2. Remove the screws that hold the back cover, then lift it off.
3. Unclip the heatsink shroud from the board.

The project notes do not record the screw count or positions, so follow the
cover itself. Work on a grounded surface: the eMMC is soldered to the board.

## Serial console

The board has four serial pads on a header next to the SoC, between the fan and
the Ethernet port. Count from the pad nearest the **fan**; that pad is number 1.

| Pad | Signal | Connect to |
|---|---|---|
| 1 | NC | nothing |
| 2 | RX (board receive) | adapter TX |
| 3 | TX (board transmit) | adapter RX |
| 4 | GND | adapter GND |

The port is **3.3 V TTL**, 8N1, at **1500000 baud**. Use a 3.3 V adapter; do
not apply 5 V. Do not connect pad 1.

The console is UART2 at `0xfe660000`: `ttyFIQ0` on the vendor kernel, `ttyS2` on
mainline, both 1500000 8N1.

Log the console with the helper in `tools/`:

```sh
tools/serial-log.py /dev/ttyUSB0 1500000 serial-console.log
```

`serial-log.py` takes the port, the baud, and the log file, in that order;
defaults are `/dev/ttyUSB0`, 1500000 baud, and `/tmp/zspace/serial-live.log`. It
reopens the port after an unplug. It is the only reader; do not run a second.

## The maskrom button

The maskrom (**bootROM**) button is on the underside of the board, next to the
flash chip, behind the warranty sticker. Breaking the sticker **voids the
warranty**.

The button puts the RK3568 bootROM into **maskrom mode**: no loader runs from
flash, and the SoC appears on USB as `2207:350a`. Maskrom is the recovery path
of last resort: use it when the eMMC loader is broken or the board does not
reach a console.

To enter maskrom:

1. Connect a Type-C cable from the host to the Type-C port next to the USB-A
   port. Do not use the port next to the power button; that one is the power
   input.
2. Hold the maskrom button down.
3. Apply power (or press the power button) while you hold it, then release.

Then drive the board with `rkdeveloptool`. Put it on your `PATH`, or set
`T2_RKDEVELOPTOOL`; `tools/t2-flash.py` finds it.

```sh
rkdeveloptool ld                          # device in maskrom/loader mode?
rkdeveloptool db <loader>.bin             # download a loader, enter loader mode
rkdeveloptool rfi                         # flash capacity
rkdeveloptool ppt                         # print the partition table
rkdeveloptool rl 0x8000 0x20000 boot.img  # read (sector-addressed)
rkdeveloptool wl <sector> image.bin       # write  <- destructive
rkdeveloptool rd                          # reset the board
```

Writes are dangerous. A bad write to the loader region (LBA `0x40`) leaves the
board maskrom-only until you restore a good loader.

## Booting from an SD card

An SD card **cannot** replace the first-stage loader: the RK3568 BootROM always
loads the SPL from the eMMC at LBA `0x40`. A raw `idbloader.img` on the card
never runs.

The next stage can come from the card. When the card carries a valid U-Boot in
its `uboot` partition, the vendor SPL loads U-Boot from the card. Otherwise it
falls back to the eMMC. This is timing-sensitive, so do not rely on "insert the
card and reboot". A plain FAT card is not a boot source.

So reach the SD path deliberately:

* The installer card carries both loaders in its `uboot` partition and a FAT
  boot tree. The board's U-Boot selects that tree when the power button is held
  at power-on. See `docs/using.md`.
* With a mainline U-Boot flashed to the eMMC, the same power-button gate selects
  the card tree. With no press, U-Boot boots the eMMC.

If the eMMC SPL itself is broken, only maskrom restores the board.
