#!/usr/bin/env python3
"""Read ZIP archives that live on a remote HTTP server, by byte range.

Vendor download servers here hand out multi-gigabyte archives (the ZSpace Z4pro
recovery image is 8.8 GiB) that nobody wants to pull down in full just to look
inside. This fetches the end-of-central-directory, then the central directory,
then only the entries you ask for - a few MiB of traffic per archive.

ZIP64 is supported (a >4 GiB archive almost always needs it). Every extraction is
verified against the CRC-32 recorded in the central directory, so a silent
truncation or a wrong offset cannot masquerade as a good extract.

Used as a library by zspace-ota.py, and directly:

    remotezip.py info  <url>
    remotezip.py list  <url> [--grep RE]
    remotezip.py get   <url> --out DIR [--grep RE] [--max-size N]
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import urllib.request
import zlib
from dataclasses import dataclass, field

CHUNK = 1 << 16


class HttpRange:
    """Byte-range reader with retries; tracks how much it actually fetched."""

    def __init__(self, url: str, retries: int = 3):
        self.url = url
        self.retries = retries
        self.bytes_read = 0
        self.size = self._size()

    def _size(self) -> int:
        req = urllib.request.Request(self.url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers["Content-Length"])

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size:
            return b""
        length = min(length, self.size - offset)
        last: Exception | None = None
        for _ in range(self.retries):
            try:
                req = urllib.request.Request(
                    self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                self.bytes_read += len(data)
                if len(data) == length:
                    return data
                last = IOError(f"short read: wanted {length}, got {len(data)}")
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = exc
        raise IOError(f"range read {offset}+{length} failed: {last}")

    def stream(self, offset: int, length: int, sink, progress: bool = True):
        done = 0
        while done < length:
            n = min(CHUNK, length - done)
            sink.write(self.read(offset + done, n))
            done += n
            if progress:
                print(f"\r{done / 2**20:9.1f} / {length / 2**20:.1f} MiB",
                      end="", flush=True)
        if progress:
            print()


@dataclass
class ZipEntry:
    name: str
    method: int
    flags: int
    crc: int
    csize: int
    usize: int
    lho: int


@dataclass
class Archive:
    origin: int          # file offset that archive offset 0 maps to
    cd_offset: int       # in archive coordinates
    cd_size: int
    eocd_offset: int     # in file coordinates
    zip64: bool = False
    entries: list = field(default_factory=list)

    def local_header(self, rf: HttpRange, entry: ZipEntry) -> tuple[int, int]:
        """(data_offset, data_length) for an entry, read from its local header."""
        head = rf.read(self.origin + entry.lho, 30)
        if head[:4] != b"PK\x03\x04":
            raise IOError(f"{entry.name}: no local header at {self.origin + entry.lho}")
        name_len, extra_len = struct.unpack_from("<HH", head, 26)
        return self.origin + entry.lho + 30 + name_len + extra_len, entry.csize

    def read_entry(self, rf: HttpRange, entry: ZipEntry) -> bytes:
        offset, length = self.local_header(rf, entry)
        raw = rf.read(offset, length)
        if entry.method == 0:
            blob = raw
        elif entry.method == 8:
            blob = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif entry.method == 12:
            raise IOError(f"{entry.name}: bzip2 method not supported here")
        else:
            raise IOError(f"{entry.name}: unsupported method {entry.method}")
        if zlib.crc32(blob) & 0xFFFFFFFF != entry.crc:
            raise IOError(f"{entry.name}: CRC mismatch after extraction")
        return blob


def _zip64_values(extra: bytes) -> list[int]:
    """Return the u64 values of the 0x0001 ZIP64 extra field, in field order."""
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        body = extra[pos + 4:pos + 4 + size]
        pos += 4 + size
        if tag != 0x0001:
            continue
        return [struct.unpack_from("<Q", body, off)[0]
                for off in range(0, len(body) - 7, 8)]
    return []


def find_eocd(rf: HttpRange, window: int = 1 << 20):
    """Locate the (ZIP64) end-of-central-directory.

    Returns (cd_end_file_offset, eocd_file_offset, zip64, cd_size, cd_offset, entries).
    `cd_end_file_offset` is where the central directory stops: with ZIP64 that is
    the ZIP64 EOCD record, not the classic one that follows it.
    """
    tail = rf.read(max(0, rf.size - window), window)
    base = max(0, rf.size - window)
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0:
        return None
    eocd = base + idx
    cd_end = eocd
    (_d, _cd, n1, n2, cd_size, cd_offset, _cl) = struct.unpack_from("<HHHHIIH", tail, idx + 4)
    zip64 = False
    if 0xFFFF in (n1, n2) or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        loc = tail.rfind(b"PK\x06\x07", 0, idx)
        if loc >= 0:
            eocd64 = struct.unpack_from("<Q", tail, loc + 8)[0]
            rec = rf.read(eocd64, 56)
            if rec[:4] == b"PK\x06\x06":
                (_sz, _vm, _vn, _dk, _cdk, _nd, n2, cd_size, cd_offset) = \
                    struct.unpack_from("<QHHIIQQQQ", rec, 4)
                zip64 = True
                cd_end = eocd64
    return cd_end, eocd, zip64, cd_size, cd_offset, n2


def parse_cd(cd: bytes, origin: int, cd_offset: int, eocd_offset: int,
             zip64: bool) -> Archive:
    arch = Archive(origin=origin, cd_offset=cd_offset, cd_size=len(cd),
                   eocd_offset=eocd_offset, zip64=zip64)
    pos = 0
    while pos + 46 <= len(cd) and cd[pos:pos + 4] == b"PK\x01\x02":
        flags, method, _mt, _md, crc, csize, usize = \
            struct.unpack_from("<HHHHIII", cd, pos + 8)
        nlen, elen, clen = struct.unpack_from("<HHH", cd, pos + 28)
        lho = struct.unpack_from("<I", cd, pos + 42)[0]
        if 0xFFFFFFFF in (usize, csize, lho):
            extra = cd[pos + 46 + nlen:pos + 46 + nlen + elen]
            # the ZIP64 extra holds only the fields that overflowed, in the
            # fixed order original-size, compressed-size, header-offset, disk
            vals = iter(_zip64_values(extra))
            if usize == 0xFFFFFFFF:
                usize = next(vals, usize)
            if csize == 0xFFFFFFFF:
                csize = next(vals, csize)
            if lho == 0xFFFFFFFF:
                lho = next(vals, lho)
        name = cd[pos + 46:pos + 46 + nlen].decode("utf-8", "replace")
        arch.entries.append(ZipEntry(name, method, flags, crc, csize, usize, lho))
        pos += 46 + nlen + elen + clen
    return arch


def open_archive(url: str, preface_origin: int | None = None) -> tuple[HttpRange, Archive]:
    """Fetch the CD of `url` and return a usable Archive.

    `preface_origin` overrides the origin calculation for containers that
    prepend their own data before the archive (see zspace-ota.py).
    """
    rf = HttpRange(url)
    found = find_eocd(rf)
    if not found:
        raise IOError(f"{url}: no ZIP end-of-central-directory found")
    cd_end, eocd, zip64, cd_size, cd_offset, _n = found
    cd_file = cd_end - cd_size
    origin = preface_origin if preface_origin is not None else cd_file - cd_offset
    arch = parse_cd(rf.read(cd_file, cd_size), origin, cd_offset, eocd, zip64)
    return rf, arch


def cmd_info(args) -> int:
    rf, arch = open_archive(args.url)
    print(f"url            {rf.url}")
    print(f"size           {rf.size:,} bytes ({rf.size / 2**30:.2f} GiB)")
    print(f"zip64          {arch.zip64}")
    print(f"origin         {arch.origin:,}")
    print(f"central dir    {arch.cd_offset:,} +{arch.cd_size:,} (archive coords)")
    print(f"eocd           {arch.eocd_offset:,}  trailing {rf.size - arch.eocd_offset - 22} bytes")
    print(f"entries        {len(arch.entries):,}")
    print(f"uncompressed   {sum(e.usize for e in arch.entries) / 2**30:.2f} GiB")
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
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"FAIL {e.name}: {exc}")
            bad += 1
            continue
        with open(dest, "wb") as f:
            f.write(blob)
        print(f"ok   {len(blob):>14,} {e.name}")
        ok += 1
    print(f"# extracted {ok}, failed {bad}, fetched {rf.bytes_read / 2**20:.1f} MiB")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("url"); p.set_defaults(func=cmd_info)
    p = sub.add_parser("list")
    p.add_argument("url"); p.add_argument("--grep"); p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("get")
    p.add_argument("url"); p.add_argument("--out", default="zip-files")
    p.add_argument("--grep"); p.add_argument("--max-size", type=int, default=16 << 20)
    p.set_defaults(func=cmd_get)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
