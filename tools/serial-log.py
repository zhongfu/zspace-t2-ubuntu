#!/usr/bin/env python3
"""Log a serial port at a given baud to a file with timestamps (binary safe).

Two things this has to survive on a real bench (both were hit 2026-10-01):
  * **USB re-enumeration.**  When the FTDI adapter drops off the bus the fd goes
    stale and `read` returns nothing forever - so a reader that only logs "read
    error" silently stops logging.  Here the port is reopened instead.
  * **Prompts.**  A getty prompt (`t2 login: `) has no trailing newline, so a
    line-buffered writer never flushes it.  Partial lines are flushed after a
    short idle, which is also what lets another process *tail this file* to
    interact with the console while this logger stays the single reader.

Usage:
    tools/serial-log.py [PORT] [BAUD] [OUT]

Defaults: PORT /dev/ttyUSB0, BAUD 1500000, OUT /tmp/zspace/serial-live.log.
"""
import argparse
import os
import termios
import time

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 1500000
DEFAULT_LOG = "/tmp/zspace/serial-live.log"

SPEEDS = {9600: termios.B9600, 38400: termios.B38400, 57600: termios.B57600,
          115200: termios.B115200, 1500000: termios.B1500000}


def open_port(port, sp):
    fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    termios.tcsetattr(fd, termios.TCSANOW,
                      [0, 0, attrs[2] | termios.CS8 | termios.CLOCAL | termios.CREAD,
                       0, sp, sp, attrs[6]])
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def stamp(chunk):
    return f"[{time.strftime('%H:%M:%S.%f')[:-3]}] ".encode() + chunk + b"\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="log a serial port to a timestamped file (binary safe)")
    ap.add_argument("port", nargs="?", default=DEFAULT_PORT,
                    help=f"serial device (default {DEFAULT_PORT})")
    ap.add_argument("baud", nargs="?", type=int, default=DEFAULT_BAUD,
                    help=f"baud rate (default {DEFAULT_BAUD})")
    ap.add_argument("out", nargs="?", default=DEFAULT_LOG,
                    help=f"log file, appended (default {DEFAULT_LOG})")
    args = ap.parse_args(argv)

    if args.baud not in SPEEDS:
        ap.error(f"unsupported baud {args.baud}; choose one of "
                 + ", ".join(str(b) for b in sorted(SPEEDS)))

    parent = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(parent, exist_ok=True)

    sp = SPEEDS[args.baud]
    try:
        fd = open_port(args.port, sp)
    except Exception as e:  # termios.error is not an OSError subclass
        raise SystemExit(f"serial-log: cannot open {args.port}: {e}")
    print(f"logging {args.port} @ {args.baud} -> {args.out}", flush=True)
    buf = b""
    with open(args.out, "ab", buffering=0) as f:
        last = time.time()
        while True:
            try:
                data = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                data = b""
            except OSError as e:
                # Adapter re-enumerated: the old fd is dead.  Reopen so logging resumes.
                print(f"read error {e} - reopening {args.port}", flush=True)
                f.write(stamp(f"serial-log: port error {e}; reopening {args.port}".encode()))
                try:
                    os.close(fd)
                except OSError:
                    pass
                time.sleep(1.0)
                while True:
                    try:
                        fd = open_port(args.port, sp)
                        break
                    except Exception:
                        time.sleep(1.0)
                buf = b""
                last = time.time()
                continue

            if data:
                last = time.time()
                buf += data
                while b"\n" in buf:
                    chunk, buf = buf.split(b"\n", 1)
                    f.write(stamp(chunk))
            elif buf and time.time() - last > 0.05:
                # No newline yet - flush the partial line so prompts show up live.
                f.write(stamp(buf))
                buf = b""
            else:
                time.sleep(0.01)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
