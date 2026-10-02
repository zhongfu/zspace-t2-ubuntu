#!/usr/bin/env python3
"""Write a FIT boot image into the ZSpace T2 `boot` partition - fully no-touch.

Sequence: (optionally) SysRq-`b` reset -> hammer CTRL+C across U-Boot's 1 s
autoboot window -> `rockusb 0 mmc 0` so U-Boot's own MMC driver backs
rkdeveloptool -> write the image in <=8 MiB chunks -> read-verify what the read
path allows -> `rd` reset.

Realities this encodes (see notes/mainline-7.3-build.md "Bring-up log 2"):
  * `reboot`/`reboot -f` from the bring-up shell HANGS the board. Reset with
    SysRq-`b` (serial BREAK then 'b') instead - that always works.
  * The `misc` BCB "bootloader" trick does NOT work: U-Boot's
    `bootcmd=boot_fit;boot_android...` runs boot_fit first, so the BCB is never
    consulted.
  * `rl` at LBA >= 0x10000 returns 0xcc through both the maskrom loader and
    U-Boot rockusb. WRITES still land (a 53 MB image boots), so only the first
    16 MiB can be read back; trust the boot for the rest.
  * CTRL+C must land inside a 1 s window, so it is hammered rather than timed.
  * A DTS change moves the kernel inside the FIT (kernel offset = 0x800 + dtb
    size), so always write the whole FIT, never just the DTB region.

Usage:
  tools/t2-flash.py --image build/out/t2-mainline-boot.img
  tools/t2-flash.py --image ... --no-reset        # board just powered on
  tools/t2-flash.py --image build/out/rootfs.ext4 --lba 0x48000
  tools/t2-flash.py --image ... --dry-run         # size/sha/chunk plan only

`--lba` defaults to 0x8000 (the `boot` partition, p3).  The distro rootfs goes
to p6 = LBA 0x48000 (294912).  A payload starting at/above 0x10000 has **no**
readable window (`rl` returns 0xcc there), so the write is verified by booting.
At 8 MiB per `wl`, a 6 GiB rootfs is 768 chunks - expect tens of minutes.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import termios
import time

TTY = os.environ.get("T2_TTY", "/dev/ttyUSB0")
LOG = os.environ.get("T2_LOG", "/tmp/zspace/serial-live.log")
# rkdeveloptool is not shipped in this repository: use $T2_RKDEVELOPTOOL or
# whatever is on PATH.
RK = os.environ.get("T2_RKDEVELOPTOOL", "rkdeveloptool")
CHUNK = 8 * 1024 * 1024    # <=8 MiB per wl
# Scratch directory for the <=8 MiB write chunks (deleted as they are written).
CHUNK_DIR = os.environ.get("T2_FLASH_CHUNKS", "/tmp/zspace/flash-chunks")
LBA_BOOT = 0x8000          # GPT: boot partition p3; p6/rootfs is 0x48000


def rk(*args, timeout=1800):
    p = subprocess.run([RK, *args], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def log_tail(n=1):
    try:
        with open(LOG, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no log)"


class LogFollower:
    """Incrementally read new lines from the serial logger's output."""

    def __init__(self, path):
        self.path = path
        self.off = os.path.getsize(path) if os.path.exists(path) else 0

    def read(self):
        try:
            with open(self.path, errors="replace") as f:
                f.seek(self.off)
                data = f.read()
                self.off = f.tell()
            return data
        except OSError:
            return ""


def into_rockusb(no_reset):
    _, state = rk("ld")
    if "Loader" in state or "Maskrom" in state:
        print(f"[=] already in download mode: {state}")
        return True

    fd = os.open(TTY, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    if not no_reset:
        print("[*] SysRq-b reset (serial BREAK + 'b')")
        termios.tcsendbreak(fd, 0)
        time.sleep(1.0)
        os.write(fd, b"b")

    # U-Boot stops autoboot only if CTRL+C lands inside its 1 s window.  A 30 ms
    # flood has been observed to *fail* (UART FIFO overrun drops it), so send
    # sparsely (~1 char per 100 ms) over a long window and watch the serial log
    # for the '=>' prompt to confirm we actually caught it.
    follower = LogFollower(LOG)
    deadline = 90.0 if no_reset else 20.0
    print(f"[*] sparse CTRL+C, up to {deadline:.0f}s, watching for the prompt")
    t0 = time.time()
    got_prompt = False
    while time.time() - t0 < deadline:
        os.write(fd, b"\x03")
        if "=>" in follower.read():
            got_prompt = True
            break
        time.sleep(0.1)
    print(f"[*] U-Boot prompt {'reached' if got_prompt else 'NOT seen'}")
    if not got_prompt:
        print(f"    last serial: {log_tail(3)}", file=sys.stderr)
    time.sleep(0.3)
    os.write(fd, b"\r")
    time.sleep(0.3)
    print("[*] => rockusb 0 mmc 0")
    os.write(fd, b"rockusb 0 mmc 0\r")
    time.sleep(3)
    os.close(fd)

    for _ in range(15):
        _, state = rk("ld")
        if "Loader" in state or "Maskrom" in state:
            print(f"[=] download mode: {state}")
            return True
        time.sleep(1)
    print(f"[!] failed to enter download mode: {state}", file=sys.stderr)
    print(f"    last serial: {log_tail(2)}", file=sys.stderr)
    return False


def sha256_stream(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def flash(image, base_lba, dry=False):
    size = os.path.getsize(image)
    nchunks = (size + CHUNK - 1) // CHUNK
    print(f"[*] {image}\n    {size} bytes ({size / 2**30:.2f} GiB)  "
          f"{nchunks} chunks of {CHUNK // 2**20} MiB  "
          f"lba {hex(base_lba)} ({base_lba})")
    print(f"    sha256 {sha256_stream(image)}")
    if dry:
        print("[dry] would write, verify the readable window and reset")
        return True

    outdir = CHUNK_DIR
    os.makedirs(outdir, exist_ok=True)

    # Streamed: a 6 GiB rootfs must never be read into memory in one go.
    with open(image, "rb") as f:
        for idx in range(nchunks):
            part = f.read(CHUNK)
            lba = base_lba + idx * (CHUNK // 512)
            cf = f"{outdir}/chunk-{base_lba:x}-{idx:04d}.bin"
            open(cf, "wb").write(part)
            rc, out = rk("wl", hex(lba), cf)
            if rc != 0 or "100%" not in out:
                print(f"[!] chunk {idx} at {hex(lba)} failed: {out}",
                      file=sys.stderr)
                return False
            print(f"    chunk {idx}/{nchunks}: lba {hex(lba)} {len(part)}B ok")
            os.unlink(cf)

    # `rl` at LBA >= 0x10000 returns 0xcc, so only a payload that starts below
    # 0x10000 has a readable window.  For p6/rootfs the proof is the boot.
    if base_lba + (2 * CHUNK) // 512 <= 0x10000:
        with open(image, "rb") as f:
            for idx in (0, 1):
                f.seek(idx * CHUNK)
                part = f.read(CHUNK)
                if not part:
                    continue
                rk("rl", hex(base_lba + idx * (CHUNK // 512)),
                   hex(len(part) // 512), "/tmp/t2flash_verify.bin")
                got = open("/tmp/t2flash_verify.bin", "rb").read()
                match = (hashlib.sha256(got[:len(part)]).hexdigest()
                         == hashlib.sha256(part).hexdigest())
                print(f"    verify readable-window chunk {idx}: "
                      f"{'MATCH' if match else 'MISMATCH'}")
    else:
        print("[i] payload starts past LBA 0x10000: no readable window, "
              "boot the board to verify")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True, help="FIT boot image to write")
    ap.add_argument("--lba", default=hex(LBA_BOOT),
                    help=f"start LBA (default {hex(LBA_BOOT)} = the `boot` "
                         "partition p3; p6/rootfs is 0x48000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print size/sha256/chunk plan and exit")
    ap.add_argument("--no-reset", action="store_true",
                    help="board was just powered on; only hammer CTRL+C")
    ap.add_argument("--no-reboot", action="store_true",
                    help="leave the board in download mode afterwards")
    args = ap.parse_args()

    try:
        base_lba = int(args.lba, 0)
    except ValueError:
        sys.exit(f"bad --lba {args.lba!r}")
    if not os.path.exists(args.image):
        sys.exit(f"no such image: {args.image}")
    if args.dry_run:
        flash(args.image, base_lba, dry=True)
        return
    if shutil.which(RK) is None and not os.path.exists(RK):
        sys.exit(f"rkdeveloptool not found: install it or set $T2_RKDEVELOPTOOL "
                 f"(tried {RK!r})")
    if not into_rockusb(args.no_reset):
        sys.exit(1)
    if not flash(args.image, base_lba):
        sys.exit(1)
    if not args.no_reboot:
        print("[*] reset:", rk("rd")[1])


if __name__ == "__main__":
    main()
