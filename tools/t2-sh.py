#!/usr/bin/env python3
"""Run shell commands on the ZSpace T2 over its busybox telnetd (LAN).

Why this exists: the serial console is shared with the log watcher, and any
foreground process that reads the tty (apt-get, chroot, ...) swallows
keystrokes, so serial-driven scripting is unreliable.  The board's telnetd
gives a clean root shell over the network instead - use the serial only for
things telnet cannot do (boot messages, flashing, the U-Boot prompt).

Usage:
  tools/t2-sh.py 'uname -a' 'ls /dev/mmcblk*'
  tools/t2-sh.py --timeout 300 'apt-get -y install foo'
  T2_HOST=172.16.1.247 tools/t2-sh.py 'echo hi'

The board runs this from its bring-up initramfs, so the applet set is limited:
prefer `busybox <applet>` for anything not normally on PATH.
"""
import argparse
import os
import socket
import sys
import time

HOST = os.environ.get("T2_HOST", "172.16.1.247")
PORT = int(os.environ.get("T2_PORT", "23"))


def _strip_telnet(b: bytes) -> str:
    """Drop IAC negotiation (busybox telnetd sends it) and decode."""
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == 0xFF and i + 1 < len(b):
            if i + 2 < len(b) and b[i + 1] == 0xFA:          # SB ... SE
                j = b.find(b"\xff\xf0", i)
                i = (j + 2) if j != -1 else len(b)
                continue
            i += 3 if b[i + 1] in (0xFB, 0xFC, 0xFD, 0xFE) else 2
            continue
        out.append(b[i])
        i += 1
    return bytes(out).decode("utf-8", "replace")


def run(cmds, timeout=60.0, quiet=False):
    s = socket.create_connection((HOST, PORT), timeout=10)
    s.settimeout(2.0)
    time.sleep(0.8)
    try:
        s.recv(8192)                      # discard the banner
    except socket.timeout:
        pass
    for c in cmds:
        s.sendall((c + "\r\n").encode())
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = s.recv(65536)
            if not d:
                break
            buf += _strip_telnet(d)
        except socket.timeout:
            if buf:
                break                      # idle after output: treat as done
        except OSError:
            break
    s.close()
    if not quiet:
        print(buf, end="" if buf.endswith("\n") else "\n")
    return buf


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commands", nargs="*", help="shell commands (one per arg)")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    if not args.commands:
        ap.print_help()
        sys.exit(2)
    run(args.commands, timeout=args.timeout)


if __name__ == "__main__":
    main()
