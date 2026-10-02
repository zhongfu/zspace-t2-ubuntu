#!/usr/bin/env python3
"""Log in on the serial console and pull diagnostics.

Run it wherever the serial logger's log file is reachable (the logger owns the
read side; this tool only writes to the tty and reads the log).

Defaults: tty /dev/ttyUSB0, log /tmp/zspace/serial-live.log.  The login
credentials are the documented root/t2 of the bring-up rootfs.
"""
import argparse
import os
import sys
import time

DEFAULT_TTY = "/dev/ttyUSB0"
DEFAULT_LOG = "/tmp/zspace/serial-live.log"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tty", default=DEFAULT_TTY,
                    help=f"serial device to write to (default {DEFAULT_TTY})")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help=f"logger's log file (default {DEFAULT_LOG})")
    args = ap.parse_args(argv)

    tty, log = args.tty, args.log

    def w(s):
        fd = os.open(tty, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
        os.write(fd, s.encode())
        os.close(fd)

    def mark():
        return os.path.getsize(log)

    def delta(m):
        with open(log, errors="replace") as fh:
            fh.seek(m)
            return fh.read()

    def step(label, data, wait):
        m = mark()
        w(data)
        time.sleep(wait)
        out = delta(m)
        print("--- %s:" % label)
        print("\n".join(out.splitlines()[-12:]))

    step("newline (prompt?)", "\r\n", 2.5)
    step("login name", "root\r", 3.0)
    step("password", "t2\r", 4.0)
    step("whoami/uptime + stuck tasks", "echo SER_OK; whoami; uptime; ps -eo pid,stat,comm,wchan:24 | grep -iE 'D|echo|sh$' | head -12; echo SER_END\r", 6.0)
    step("dmesg tail", "dmesg | tail -12; echo SER_END\r", 6.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
