#!/usr/bin/env python3
"""Pull a file from the ZSpace T2 back to the host over the serial console.

    t2-recv.py <board-path> <host-path> [serial-log] [tty]

Asks the board to `base64` the file between two markers, then reassembles it
from the serial logger's log (the logger owns the port's read side, so this
tool only writes to the tty).  Each logged line carries a `[HH:MM:SS] ` prefix
which is stripped before decoding.

Defaults: serial-log /tmp/zspace/serial-live.log, tty /dev/ttyUSB0.
"""
import argparse
import base64
import os
import re
import sys
import time

DEFAULT_LOG = "/tmp/zspace/serial-live.log"
DEFAULT_TTY = "/dev/ttyUSB0"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("remote", help="path of the file on the board")
    ap.add_argument("local", help="host path to write")
    ap.add_argument("serial_log", nargs="?", default=DEFAULT_LOG,
                    help=f"logger's log file (default {DEFAULT_LOG})")
    ap.add_argument("tty", nargs="?", default=DEFAULT_TTY,
                    help=f"serial device to write to (default {DEFAULT_TTY})")
    args = ap.parse_args(argv)

    remote, local = args.remote, args.local
    log, tty = args.serial_log, args.tty

    mark = os.path.getsize(log)
    fd = os.open(tty, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    os.write(fd, ("echo T2B64_START; base64 -w 76 %s; echo T2B64_END\r\n" % remote).encode())
    os.close(fd)

    prefix = re.compile(r"^\[\d\d:\d\d:\d\d\] ?")
    lines, started = [], False
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(1)
        with open(log, "r", errors="replace") as fh:
            fh.seek(mark)
            for raw in fh:
                line = prefix.sub("", raw.rstrip("\n"))
                if "T2B64_START" in line:
                    started, lines = True, []
                    continue
                if "T2B64_END" in line:
                    data = base64.b64decode("".join(lines))
                    with open(local, "wb") as out:
                        out.write(data)
                    print("received %d bytes -> %s" % (len(data), local))
                    return 0
                if started and line and not line.startswith("base64:"):
                    lines.append(line)
    print("timed out waiting for T2B64_END (got %d lines)" % len(lines))
    return 1


if __name__ == "__main__":
    sys.exit(main())
