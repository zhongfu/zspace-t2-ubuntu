#!/usr/bin/env python3
"""Dev-host side of the T2 reverse shell.

The T2 is behind an Android hotspot: the board can reach the dev host, the dev
host cannot reach the board.  So the board dials out (t2-revshell-board.py) and
this tool splices that callback to a loopback port, where ordinary clients -
including this same file in `run` mode - can talk to it.

    t2-revshell.py serve [--listen IP:PORT] [--local IP:PORT]

        Wait for the board's callback, then serve the bridged shell on the
        local address.  Repeated `run` invocations reuse one board connection;
        if the board's link drops, it goes back to waiting for a new callback.

    t2-revshell.py run COMMAND [--local IP:PORT] [--timeout SECS]

        Drive the bridged shell: send COMMAND, wait for its sentinel, print the
        output between the sentinels.  Exit status is the command's own.

    t2-revshell.py shell

        Just splice stdin/stdout to the bridged shell (for eyeballing).

Defaults: --listen 172.16.1.204:4444, --local 127.0.0.1:4445.
"""
import argparse
import re
import select
import socket
import sys
import threading
import time

START = "T2R_START"
END = "T2R_END"
# `echo T2R_END$?` emits the marker glued to the status, e.g. "T2R_END0".
END_RE = re.compile(rb"T2R_END(-?\d+)")


def split(addr):
    host, _, port = addr.rpartition(":")
    return host, int(port)


def listen(addr):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    return s


def splice(a, b):
    """Pump bytes both ways until one side closes.  Returns the closed side."""
    while True:
        r, _, _ = select.select([a, b], [], [], 60)
        for s in r:
            data = s.recv(65536)
            if not data:
                return s
            (b if s is a else a).sendall(data)


def serve(args):
    listen_addr = split(args.listen)
    local_addr = split(args.local)
    ls = listen(listen_addr)
    print("waiting for board callback on %s:%d" % listen_addr, flush=True)

    board = None
    while True:
        if board is None:
            board, peer = ls.accept()
            print("board connected from %s:%d" % peer, flush=True)

        ll = listen(local_addr)
        print("bridged shell ready on %s:%d" % local_addr, flush=True)
        client, cpeer = ll.accept()
        print("client %s:%d attached" % cpeer, flush=True)

        closed = splice(board, client)
        client.close()
        ll.close()

        if closed is board:
            print("board link dropped", flush=True)
            board.close()
            board = None


def run(args):
    cmd = args.command
    s = socket.create_connection(split(args.local), timeout=args.timeout)
    s.sendall(("echo %s; %s; echo %s$?\n" % (START, cmd, END)).encode())

    buf = b""
    match = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        s.settimeout(max(0.5, deadline - time.time()))
        try:
            data = s.recv(65536)
        except (socket.timeout, TimeoutError):
            break
        if not data:
            break
        buf += data
        match = END_RE.search(buf)
        if match:
            break
    s.close()

    head = buf[: match.start()] if match else buf
    text = head.decode(errors="replace")
    if START in text:
        text = text.split(START, 1)[1]

    if match:
        status = match.group(1).decode()
    else:
        status = "?"
        print("t2-revshell: no end marker (timeout?)", file=sys.stderr)

    body = text.strip("\n")
    if body:
        sys.stdout.write(body + "\n")
    try:
        return int(status)
    except ValueError:
        return 1


def shell(args):
    s = socket.create_connection(split(args.local), timeout=args.timeout)

    def pump(src, dst):
        while True:
            data = src.recv(65536)
            if not data:
                return
            dst.write(data)
            dst.flush()

    t = threading.Thread(target=pump, args=(s, sys.stdout.buffer), daemon=True)
    t.start()
    while True:
        data = sys.stdin.buffer.read(1)
        if not data:
            break
        s.sendall(data)
    return 0


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--listen", default="172.16.1.204:4444")
    common.add_argument("--local", default="127.0.0.1:4445")
    common.add_argument("--timeout", type=float, default=60.0)

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve", parents=[common])
    r = sub.add_parser("run", parents=[common])
    r.add_argument("command")
    sub.add_parser("shell", parents=[common])
    args = p.parse_args()

    fn = {"serve": serve, "run": run, "shell": shell}[args.mode]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
