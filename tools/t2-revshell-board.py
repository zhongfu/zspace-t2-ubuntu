#!/usr/bin/env python3
"""T2 side of the reverse shell: dial the dev host and serve shells on it.

Run it on the board (deliver it however is convenient - it is small, and the
board can always reach the dev host, see t2-revshell.py):

    curl -s -o /tmp/rev.py http://<devhost>:8099/t2-revshell-board.py
    setsid nohup python3 /tmp/rev.py >/tmp/rev.log 2>&1 &

Addresses come from the environment (or --host/--port) so the one-liner stays
quotable:

    T2_REV_HOST (default 172.16.1.204)   T2_REV_PORT (default 4444)

It retries the connection forever (3 s apart) so it does not matter which side
comes up first, and it is a **supervisor**: when a shell exits - or the link
drops, which the benchmark hotspot does routinely (observed as
`brcmf_msgbuf_query_dcmd: Timeout on response for query command` followed by a
reset) - it dials again and starts a fresh shell instead of leaving the board
unreachable.  An `exec`-once design was measured to lose the board permanently
on the first blip.

No pty: the shell is line-oriented and does not echo its input, which is what
makes sentinel-driven command execution exact - see `t2-revshell.py run`.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

DEFAULT_HOST = os.environ.get("T2_REV_HOST", "172.16.1.204")
DEFAULT_PORT = int(os.environ.get("T2_REV_PORT", "4444"))


def dial(host, port):
    while True:
        s = socket.socket()
        try:
            s.connect((host, port))
            # The shell is a long-lived interactive-ish session; do not let the
            # kernel time it out during a quiet patch.
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError as exc:
            print("connect %s:%d failed (%s); retrying" % (host, port, exc),
                  flush=True)
            s.close()
            time.sleep(3)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"dev host to dial (default {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"dev host port (default {DEFAULT_PORT})")
    args = ap.parse_args(argv)

    while True:
        sock = dial(args.host, args.port)
        print("connected to %s:%d" % (args.host, args.port), flush=True)
        try:
            proc = subprocess.Popen(["/bin/bash"], stdin=sock, stdout=sock,
                                    stderr=sock, close_fds=True)
            proc.wait()
            print("shell exited with %s" % proc.returncode, flush=True)
        except OSError as exc:
            print("spawn failed: %s" % exc, flush=True)
        finally:
            sock.close()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
