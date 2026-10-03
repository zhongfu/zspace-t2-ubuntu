# tools — helpers for a live T2

User-facing helpers for a running ZSpace T2.  Each tool resolves its paths from
its own location and runs from any clone path.  Python tools answer `--help`;
shell tools answer `-h`/`--help`.

## Serial console

The T2 debug UART runs at **1500000 baud** on the FTDI adapter's first port,
`/dev/ttyUSB0`.  An 8-N-1 connection, no flow control.  The **logger owns the
read side**; the other tools write only and read the reply back out of the
logger's log, so exactly one process reads the port.  Defaults are
`/dev/ttyUSB0`, 1500000 baud and log `/tmp/zspace/serial-live.log`.  The bring-up
rootfs logs in as `root` / `t2`.  Adapter and cabling details are in
`docs/tinkering.md`.

| Tool | One line |
|---|---|
| `serial-log.py` | append a timestamped, binary-safe log of the serial port (survives USB re-enumeration) |
| `t2-send.py` | run one shell command on the board over the console; print its output |
| `t2-recv.py` | pull a file from the board to the host over the console (base64) |
| `t2-serial-login.py` | log in on the console and print diagnostics (uptime, stuck tasks, dmesg) |

## Board shells over the network

| Tool | One line |
|---|---|
| `t2-sh.py` | run commands on the board's busybox telnetd (LAN), `T2_HOST`/`T2_PORT` |
| `t2-revshell.py` | dev-host side of the reverse shell: `serve`, `run`, `shell` modes |
| `t2-revshell-board.py` | board side of the reverse shell: dial the dev host and serve shells |

## USB gadget link

| Tool | One line |
|---|---|
| `t2-gadget-link.py` | bring up the host side of the USB-C CDC-NCM link (10.55.55.2) and verify the board answers |

## GPIO

| Tool | One line |
|---|---|
| `t2-gpio.py` | drive one GPIO line through the character-device v2 ioctl ABI (no libgpiod) |

## Flash and rescue

| Tool | One line |
|---|---|
| `t2-flash.py` | write a FIT (or any image) into the eMMC over rockusb, no-touch; needs `rkdeveloptool` on `PATH` or `$T2_RKDEVELOPTOOL` |

## Firmware dump and image inspection

| Tool | One line |
|---|---|
| `zspace-fetch.sh` | pull a firmware dump off a rooted NAS over ssh, compressed and hash-verified |
| `zspace-dump.sh` | device-side read-only dumper: device/partition reports and raw byte ranges |
| `rk-extract.py` | take a dump apart: GPT, containers and device trees, written under `--out` |
| `rkimg.py` (in `lib/`) | shared Rockchip parser module; run `python3 lib/rkimg.py image` to identify one |

## Releasing

CI runs these from `.github/workflows/`.  `docs/releasing.md` explains the flow.

| Tool | One line |
|---|---|
| `collect-release-artifacts.sh` | copy a finished `build/out/` into the release files, find the `t2-utils` `.deb` by glob, write `SHA256SUMS` and the notes file list |
| `ci-fetch-firmware.sh` | download the vendor firmware archive from `T2_FIRMWARE_URL` and install it with `rootfs/fetch.sh --from-dir` |
