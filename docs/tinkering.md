# Tinkering with the ZSpace T2

This page covers the physical side of the board: opening the case, the serial
console, the maskrom button, and booting from an SD card.  Read
`docs/building.md` to build the images, and `docs/using.md` to install and use
them.

## Opening the case

1. Power the board off and unplug the power supply.
2. Remove the screws that hold the back cover, then lift the cover off.
3. Unclip the heatsink shroud from the board.

The project notes do not record the screw count or their positions, so follow
the cover itself.  Work on a grounded surface: the eMMC is soldered to the
board.

## Serial console

The board has four serial pads on a header next to the SoC, between the fan and
the Ethernet port.  Count from the pad nearest the **fan**; that pad is number 1.

| Pad | Signal | Connect to |
|---|---|---|
| 1 | NC | nothing |
| 2 | RX (board receive) | adapter TX |
| 3 | TX (board transmit) | adapter RX |
| 4 | GND | adapter GND |

The port is **3.3 V TTL**, 8N1, at **1500000 baud**.  Use a 3.3 V adapter; do
not apply 5 V.  Do not connect pad 1.

The console is UART2, base address `0xfe660000`.  The vendor kernel calls this
port `ttyFIQ0` (Rockchip's FIQ debugger, which defaults to 1500000).  Mainline
Linux calls it `ttyS2` and runs the same 1500000 8N1.

Log the console with the helper in `tools/`:

```sh
tools/serial-log.py /dev/ttyUSB0 1500000 serial-console.log
```

`serial-log.py` takes the port, the baud, and the log file, in that order.  Its
defaults are `/dev/ttyUSB0`, 1500000 baud, and `/tmp/zspace/serial-live.log`.  It
reopens the port when the USB adapter re-enumerates, so logging survives an
unplug.  It is the only reader of the port; do not run a second reader on the
same device.

## The maskrom button

The maskrom (**bootROM**) button is on the underside of the board, next to the
flash chip (the RK3568 SoC is on the top side).  It sits **behind the warranty
sticker**.
Breaking the sticker **voids the warranty**.  Decide before you peel it.

The button puts the RK3568 bootROM into **maskrom mode**.  In this mode the SoC
does not run any loader from flash.  Instead it presents itself on USB as
`2207:350a`.  Maskrom is the recovery path of last resort: it works when the
eMMC loader is broken or the board does not reach a console.

To enter maskrom:

1. Connect a Type-C cable from the host to the Type-C port **next to the USB-A
   port**.  Do not use the Type-C port next to the power button; that one is the
   power input.
2. Hold the maskrom button down.
3. Apply power (or press the power button) while you hold it, then release.

Then drive the board with `rkdeveloptool`, the standard Rockchip host utility.
Put it on your `PATH`; `tools/t2-flash.py` finds it there, or from the
`T2_RKDEVELOPTOOL` environment variable.

```sh
rkdeveloptool ld                          # device in maskrom/loader mode?
rkdeveloptool db <loader>.bin             # download a loader, enter loader mode
rkdeveloptool rfi                         # flash capacity
rkdeveloptool ppt                         # print the partition table
rkdeveloptool rl 0x8000 0x20000 boot.img  # read (sector-addressed)
rkdeveloptool wl <sector> image.bin       # write  <- destructive
rkdeveloptool rd                          # reset the board
```

Writes are dangerous.  A bad write to the loader region (LBA `0x40`) leaves the
board maskrom-only until you restore a known-good loader.

## Booting from an SD card

An SD card **cannot** replace the first-stage loader.  The RK3568 BootROM always
loads the SPL from the eMMC at LBA `0x40`.  A raw `idbloader.img` on the card
never runs.

What does work is the next stage.  The vendor SPL probes the SD controller and,
when the card carries a valid U-Boot in its `uboot` partition, loads **U-Boot
proper from the card** before falling back to the eMMC.  This was measured on
the board (experiment E1): with such a card inserted, the
console showed the vendor SPL, then `U-Boot 2026.07` from the card, then the
card's `extlinux.conf`.

Two limits:

* The SD preference is **timing-sensitive**.  Two warm reboots fell back to the
  eMMC even with a byte-exact card.  Do not rely on "insert the card and
  reboot".
* A plain FAT card is not a boot source.  A scan of a card with one vfat
  partition found no loader and no FIT, and the board stayed on the eMMC.

So the SD path is reached deliberately, not by the medium alone:

* The installer card carries both loaders in its `uboot` partition and a FAT
  boot tree.  The board's U-Boot selects that tree when the power button is held
  at power-on.  See `docs/using.md`.
* With a mainline U-Boot flashed to the eMMC, the same power-button gate selects
  the card tree (`bootflow scan -lb mmc1`); with no press, U-Boot boots the eMMC
  tree.

If the eMMC SPL itself is broken, only maskrom restores the board.
