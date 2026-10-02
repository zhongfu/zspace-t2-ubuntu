#!/usr/bin/env python3
"""ZSpace T2 power-button policy (t2-powerkey.service).

Policy: a *short* press of the power button is a no-op; a press held at least
`press_seconds` (default 3 s) asks systemd for a graceful poweroff.  A much
longer hold never reaches this script - the RK809 PMIC cuts the rails itself
(the PMIC's own long-press timeout), which is the last-resort power cut.

Why userspace: the PMIC's PWRON input device (rk805-pwrkey) reports only
press/release - it has no notion of press duration - and systemd-logind's
HandlePowerKey=poweroff would turn *every* touch into a poweroff.  The
logind drop-in /etc/systemd/logind.conf.d/10-t2.conf gives the key to this
service instead (HandlePowerKey=ignore).

Config: /etc/t2/powerkey.conf (device name as reported by EVIOCGNAME, key
code, press_seconds).  An empty `device=` falls back to the first input
device that can emit `code`, mirroring the initramfs press gate.

Test hooks (host-side tests only): T2_POWERKEY_CONF overrides the config
path, T2_POWERKEY_INPUT_DIR the /dev/input directory, T2_POWERKEY_SYSTEMCTL
the systemctl binary.  `--simulate` reads "<press_seconds> <release_seconds>"
pairs from stdin and prints the decision for each instead of touching any
device (implies --dry-run).
"""

import fcntl
import glob
import os
import struct
import subprocess
import sys
import time

EV_KEY = 1
KEY_POWER = 116
INPUT_EVENT = struct.Struct("llHHi")  # struct input_event, LP64: 24 bytes
KEY_BITMAP_BYTES = 96  # KEY_MAX is 0x2ff

DEF_CONF = "/etc/t2/powerkey.conf"
INPUT_DIR = os.environ.get("T2_POWERKEY_INPUT_DIR", "/dev/input")
DEFAULTS = {
    "device": "rk805 pwrkey",
    "code": str(KEY_POWER),
    "press_seconds": "3",
}


def _ioc(direction, kind, nr, size):
    return (direction << 30) | (size << 16) | (kind << 8) | nr


def eviocgname(length):
    return _ioc(2, ord("E"), 0x06, length)  # _IOR('E', 0x06, char[length])


def eviocgbit(ev, length):
    return _ioc(2, ord("E"), 0x20 + ev, length)


def log(message):
    print("t2-powerkey: %s" % message, flush=True)


def read_conf(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    except FileNotFoundError:
        log("no config at %s, using defaults" % path)
    return cfg


def device_name(fd):
    buf = fcntl.ioctl(fd, eviocgname(256), b"\0" * 256)
    return buf.split(b"\0", 1)[0].decode("utf-8", "replace")


def device_has_key(fd, code):
    bits = fcntl.ioctl(fd, eviocgbit(EV_KEY, KEY_BITMAP_BYTES),
                       b"\0" * KEY_BITMAP_BYTES)
    return bool(bits[code // 8] >> (code % 8) & 1)


def open_device(want, code):
    """Open the matching event device, or None."""
    for path in sorted(glob.glob(os.path.join(INPUT_DIR, "event*"))):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            name = device_name(fd)
            if want:
                if name == want:
                    return fd, path, name
            elif device_has_key(fd, code):
                return fd, path, name
        except OSError:
            pass
        os.close(fd)
    return None


def action_for(duration, press_seconds):
    return "poweroff" if duration >= press_seconds else "ignore"


def issue_poweroff(systemctl, dry_run):
    if dry_run:
        log("dry-run: would run %s poweroff" % systemctl)
        return
    try:
        rc = subprocess.call([systemctl, "poweroff"])
        log("systemctl poweroff -> rc %d" % rc)
    except OSError as exc:
        log("cannot run %s poweroff: %s" % (systemctl, exc))


def simulate(press_seconds, stream):
    """Read 'press release' second-pairs, print each decision."""
    for lineno, line in enumerate(stream, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pressed, released = (float(v) for v in line.split())
        except ValueError:
            log("simulate: line %d: expected '<press_seconds> "
                "<release_seconds>'" % lineno)
            return 2
        if released < pressed:
            return 2
        log("%.3f s held -> %s" % (released - pressed,
                                   action_for(released - pressed,
                                              press_seconds)))
    return 0


def run_live(want, code, press_seconds, systemctl, dry_run):
    logged_missing = False
    while True:
        opened = open_device(want, code)
        if opened is None:
            if not logged_missing:
                log("waiting for %s" % (want or "a device reporting code %d" % code))
                logged_missing = True
            time.sleep(2)
            continue
        fd, path, name = opened
        log("watching %s (%s), press >= %.1f s powers off"
            % (name, path, press_seconds))
        logged_missing = False
        pressed_at = None
        powered_off = False
        try:
            while True:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    time.sleep(0.05)
                    continue
                if not data:
                    raise OSError("device closed")
                for off in range(0, len(data) - INPUT_EVENT.size + 1,
                                 INPUT_EVENT.size):
                    _, _, etype, ecode, value = INPUT_EVENT.unpack_from(data, off)
                    if etype != EV_KEY or ecode != code:
                        continue
                    if value == 1:
                        pressed_at = time.monotonic()
                    elif value == 0 and pressed_at is not None:
                        held = time.monotonic() - pressed_at
                        pressed_at = None
                        action = action_for(held, press_seconds)
                        if action == "poweroff" and not powered_off:
                            powered_off = True
                            log("held %.1f s -> poweroff" % held)
                            issue_poweroff(systemctl, dry_run)
                        elif action != "poweroff":
                            log("held %.1f s -> ignored (short press)" % held)
        except OSError as exc:
            log("%s: %s - rescanning" % (path, exc))
            os.close(fd)
            time.sleep(2)


def main(argv):
    dry_run = "--dry-run" in argv
    simulate_mode = "--simulate" in argv
    cfg = read_conf(os.environ.get("T2_POWERKEY_CONF", DEF_CONF))
    try:
        code = int(cfg["code"], 0)
        press_seconds = float(cfg["press_seconds"])
    except ValueError as exc:
        log("bad config: %s" % exc)
        return 2
    if simulate_mode:
        return simulate(press_seconds, sys.stdin)
    run_live(cfg["device"], code, press_seconds,
             os.environ.get("T2_POWERKEY_SYSTEMCTL", "/usr/bin/systemctl"),
             dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
