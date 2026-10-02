#!/usr/bin/env python3
"""Run one shell command on the ZSpace T2 over the serial console.

    t2-send.py <command> [serial-log] [tty]

Counterpart to t2-recv.py.  The logger owns the port's read side, so this tool
only writes to the tty (one write-only fd, closed immediately) and then reads
the command's output back out of the logger's log between two markers.  Kernel
console lines (``[ 123.456] ...``) are dropped from the reply.

Defaults: serial-log /tmp/zspace/serial-live.log, tty /dev/ttyUSB0.

Environment:
    T2S_TIMEOUT  seconds to wait for the end marker (default 30)
    T2S_ALL      set to keep kernel console lines in the reply
"""
import argparse
import os
import re
import sys
import time

DEFAULT_LOG = "/tmp/zspace/serial-live.log"
DEFAULT_TTY = "/dev/ttyUSB0"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", help="shell command to run on the board")
    ap.add_argument("serial_log", nargs="?", default=DEFAULT_LOG,
                    help=f"logger's log file (default {DEFAULT_LOG})")
    ap.add_argument("tty", nargs="?", default=DEFAULT_TTY,
                    help=f"serial device to write to (default {DEFAULT_TTY})")
    args = ap.parse_args(argv)

    log, tty = args.serial_log, args.tty
    cmd = args.command
    timeout = float(os.environ.get("T2S_TIMEOUT", "30"))
    keep_kernel = bool(os.environ.get("T2S_ALL"))

    # Marker strings must be distinct from anything the command itself prints.
    start, end = "T2S_START", "T2S_END"
    mark = os.path.getsize(log)
    fd = os.open(tty, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    os.write(fd, ("echo %s; %s; echo %s\r\n" % (start, cmd, end)).encode())
    os.close(fd)

    prefix = re.compile(r"^\[\d\d:\d\d:\d\d\] ?")
    kernel = re.compile(r"^\[\s*\d+\.\d+\]")
    out, started = [], False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        with open(log, "r", errors="replace") as fh:
            fh.seek(mark)
            for raw in fh:
                line = prefix.sub("", raw.rstrip("\n"))
                if start in line:
                    started, out = True, []
                    continue
                if end in line:
                    print("\n".join(out))
                    return 0
                if started and line:
                    if kernel.match(line) and not keep_kernel:
                        continue
                    out.append(line)
    print("timed out after %.0fs waiting for %s; got %d lines:" % (timeout, end, len(out)))
    print("\n".join(out[-40:]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
