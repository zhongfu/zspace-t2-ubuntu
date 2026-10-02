#!/usr/bin/env python3
"""Read ZSpace `.zspace` OTA packages straight over HTTP byte ranges.

Container layout (measured on
/nas/offline/p080/release/1761118676/p080_release_202510221537.zspace):

    [0 .. P)              base64 ASCII block, decodes to a small opaque header
    [P .. Z)              high-entropy blob with no recognisable magic - not
                          readable without a vendor key, so it is left alone
    [Z .. eocd)           a plain, unencrypted ZIP archive (the service layer)
    [eocd .. EOF)         short ASCII metadata, e.g. "1368_11608_189584108,..."

A package can hold more than one sub-archive (the p080 file carries a
P080_SYSTEM_* and a P080_SERVICE_* ZIP); each archive's internal offsets are
relative to its own start, recovered as

    origin = (cd_end_file_offset - cd_size) - cd_offset

The first ~17 KiB of the SERVICE archive is overwritten by the container
preface, so its earliest entries have no local header; every extraction is
therefore verified against the CRC-32 in the central directory and unreadable
entries are reported rather than silently skipped.

Only byte ranges are fetched: a 1.6 GiB package costs a few MiB of traffic.

    zspace-ota.py info   <url>
    zspace-ota.py list   <url> [--grep RE]
    zspace-ota.py get    <url> --out DIR [--grep RE] [--max-size N]
    zspace-ota.py prefix <url> --out FILE     (the opaque region, for offline work)
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from remotezip import HttpRange, find_eocd, open_archive  # noqa: E402

B64 = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n")


def base64_preface(rf: HttpRange, probe: int = 1 << 16) -> tuple[int, int]:
    """(length in bytes, decoded length) of the ASCII block at offset 0."""
    head = rf.read(0, probe)
    idx = 0
    while idx < len(head) and head[idx] in B64:
        idx += 1
    block = head[:idx]
    end = block.rfind(b"=") + 1
    if end <= 0:
        return 0, 0
    try:
        return end, len(base64.b64decode(block[:end]))
    except Exception:  # noqa: BLE001
        return end, 0


def archive_origin(rf: HttpRange) -> int | None:
    found = find_eocd(rf)
    if not found:
        return None
    cd_end, _eocd, _z, cd_size, cd_offset, _n = found
    return (cd_end - cd_size) - cd_offset


def cmd_info(args) -> int:
    rf = HttpRange(args.url)
    print(f"url            {rf.url}")
    print(f"size           {rf.size:,} bytes ({rf.size / 2**30:.2f} GiB)")
    plen, dlen = base64_preface(rf)
    print(f"ascii preface  {plen:,} bytes (decodes to {dlen} opaque bytes)")
    origin = archive_origin(rf)
    if origin is None:
        print("no ZIP end-of-central-directory: container is entirely opaque")
        return 0
    rf, arch = open_archive(args.url)
    print(f"zip origin     {origin:,} (file offset that archive offset 0 maps to)")
    print(f"zip cd         {arch.cd_offset:,} +{arch.cd_size:,} (archive coords)")
    print(f"zip eocd       {arch.eocd_offset:,}   trailing metadata "
          f"{rf.size - arch.eocd_offset - 22} bytes")
    print(f"entries        {len(arch.entries):,}")
    print(f"uncompressed   {sum(e.usize for e in arch.entries) / 2**30:.2f} GiB")
    if origin > plen:
        print(f"opaque region  {plen:,} .. {origin:,} "
              f"({(origin - plen) / 2**20:.1f} MiB, high entropy, no known magic)")
    trailer = rf.read(arch.eocd_offset + 22, min(256, rf.size - arch.eocd_offset - 22))
    if trailer.strip(b"\x00"):
        print(f"trailer        {trailer!r}")
    return 0


def cmd_list(args) -> int:
    rf, arch = open_archive(args.url)
    pat = re.compile(args.grep, re.I) if args.grep else None
    shown = 0
    for e in arch.entries:
        if pat and not pat.search(e.name):
            continue
        print(f"{e.usize:>14,} m{e.method} {e.name}")
        shown += 1
        if args.limit and shown >= args.limit:
            break
    print(f"# {shown} of {len(arch.entries)} entries")
    return 0


def cmd_get(args) -> int:
    rf, arch = open_archive(args.url)
    pat = re.compile(args.grep, re.I) if args.grep else None
    os.makedirs(args.out, exist_ok=True)
    ok = bad = 0
    for e in arch.entries:
        if pat and not pat.search(e.name):
            continue
        if e.usize == 0 or e.name.endswith("/"):
            continue
        if e.usize > args.max_size:
            print(f"skip (too big) {e.usize:>14,} {e.name}")
            continue
        dest = os.path.join(args.out, e.name)
        os.makedirs(os.path.dirname(dest) or args.out, exist_ok=True)
        try:
            blob = arch.read_entry(rf, e)
        except Exception as exc:  # noqa: BLE001 - preface-damaged entries expected
            print(f"FAIL {e.name}: {exc}")
            bad += 1
            continue
        with open(dest, "wb") as f:
            f.write(blob)
        print(f"ok   {len(blob):>14,} {e.name}")
        ok += 1
    print(f"# extracted {ok}, failed {bad}, fetched {rf.bytes_read / 2**20:.1f} MiB")
    return 0 if ok else 1


def cmd_prefix(args) -> int:
    rf = HttpRange(args.url)
    plen, _ = base64_preface(rf)
    origin = archive_origin(rf)
    if origin is None:
        raise SystemExit("no ZIP archive found; nothing to delimit the prefix")
    length = origin - plen
    with open(args.out, "wb") as f:
        rf.stream(plen, length, f)
    print(f"wrote {args.out} ({os.path.getsize(args.out):,} bytes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("url"); p.set_defaults(func=cmd_info)
    p = sub.add_parser("list")
    p.add_argument("url"); p.add_argument("--grep"); p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("get")
    p.add_argument("url"); p.add_argument("--out", default="ota-files")
    p.add_argument("--grep"); p.add_argument("--max-size", type=int, default=8 << 20)
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("prefix")
    p.add_argument("url"); p.add_argument("--out", default="prefix.bin")
    p.set_defaults(func=cmd_prefix)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
