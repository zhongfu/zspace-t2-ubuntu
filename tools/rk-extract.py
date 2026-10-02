#!/usr/bin/env python3
"""rk-extract - take a Rockchip device dump apart.

Input is either
  * a whole-device image (the output of `zspace-dump.sh full ...`, or a maskrom
    read), in which case partitions are located from the GPT / `parameter`
    partition and analysed in place, or
  * any single partition/container image (boot.img, resource.img, uboot.img,
    a bare .dtb, ...) dumped by `zspace-dump.sh part ...`.

Everything found is written under --out, plus report.txt and manifest.json.

The point of this tool is to answer "what firmware is on this box" and to hand
back the device tree sources. Container formats are parsed by rkimg.py; the
device tree is only *located* here and decompiled by dtc, which is the authority
on DTB syntax.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
# The shared Rockchip parser module lives in <repo>/lib, imported by path.
sys.path.insert(0, str(ROOT / "lib"))
import rkimg  # noqa: E402
from rkimg import SECTOR, Partition  # noqa: E402

# dtc decompiles the device trees this tool locates; take it from $DTC or PATH.
# When it is absent, the DTBs are still saved and decompilation is skipped.
DTC = Path(os.environ.get("DTC") or shutil.which("dtc") or "dtc")

SCAN_HEAD = 32 * 1024 * 1024      # how much of a slice to identify / scan for FDT


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def _decompress(blob: bytes, algo: str) -> tuple[bytes, str | None]:
    """Apply a FIT `compression` value. Returns (bytes, note-or-None)."""
    if algo in ("", "none"):
        return blob, None
    try:
        if algo == "gzip":
            return gzip.decompress(blob), None
        if algo == "lzma":
            try:
                return lzma.decompress(blob, format=lzma.FORMAT_ALONE), None
            except lzma.LZMAError:
                return lzma.decompress(blob), None
        if algo == "lz4":
            return blob, "lz4 payload left compressed (no lz4 module)"
        if algo == "zstd":
            return blob, "zstd payload left compressed (no zstd module)"
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return blob, f"{algo} decompression failed: {exc}"
    return blob, f"unknown compression {algo}"


class Extractor:
    def __init__(self, out: Path, carve: str = "small", carve_limit: int = 256 << 20,
                 no_dtc: bool = False, only: set[str] | None = None):
        self.out = out
        self.carve = carve
        self.carve_limit = carve_limit
        self.no_dtc = no_dtc
        self.only = only
        self.report: list[str] = []
        self.manifest: dict = {"images": []}

    # ---------------------------------------------------------------- logging
    def say(self, msg: str = "") -> None:
        self.report.append(msg)
        print(msg)

    # ------------------------------------------------------------------- dtbs
    def _decompile(self, dtb: Path, label: str, info: dict) -> dict:
        """Run dtc on an extracted DTB and pull model/compatible out of the DTS."""
        if self.no_dtc or not DTC.exists():
            return info
        dts = self.out / f"{label}.dts"
        r = subprocess.run([str(DTC), "-I", "dtb", "-O", "dts", "-o", str(dts),
                            str(dtb)], capture_output=True, text=True)
        if r.returncode != 0:
            self.say(f"    dtc failed on {label}: {r.stderr.strip()}")
            info["dtc_error"] = r.stderr.strip()
            return info
        info["dts"] = str(dts.relative_to(self.out))
        text = dts.read_text(errors="replace")
        for key in ("model", "compatible"):
            m = re.search(rf'^\s*{key} = "([^"]+)"', text, re.M)
            if m:
                info[key] = m.group(1)
        self.say(f"    DTS  {info['dts']}"
                 + (f"   model={info.get('model')}" if info.get("model") else ""))
        return info

    def save_fdt(self, img, base: int, label: str, container: str) -> dict | None:
        total = rkimg.fdt_totalsize(img, base)
        if total <= 0 or base + total > len(img):
            return None
        dtb = self.out / f"{label}.dtb"
        with open(dtb, "wb") as f:
            f.write(img[base:base + total])
        info = {"dtb": str(dtb.relative_to(self.out)), "size": total,
                "container": container}
        self.say(f"    DTB  {info['dtb']} ({human(total)})")
        return self._decompile(dtb, label, info)

    def save_fdt_bytes(self, blob: bytes, label: str, container: str) -> dict | None:
        """Same as save_fdt() for a DTB that only exists in memory (FIT payload)."""
        total = rkimg.fdt_totalsize(blob, 0)
        if total <= 0 or total > len(blob):
            return None
        dtb = self.out / f"{label}.dtb"
        dtb.write_bytes(blob[:total])
        info = {"dtb": str(dtb.relative_to(self.out)), "size": total,
                "container": container}
        self.say(f"    DTB  {info['dtb']} ({human(total)})")
        return self._decompile(dtb, label, info)

    def scan_fdts(self, img, base: int, size: int, label: str, container: str,
                  depth: int = 0) -> list[dict]:
        """Find DTBs in a bounded slice. `depth` guards against recursion blowup."""
        if depth > 2 or size <= 0:
            return []
        window = min(size, SCAN_HEAD)
        slice_ = img[base:base + window]
        found = []
        for off in rkimg.fdt_candidates(slice_):
            info = self.save_fdt(img, base + off, f"{label}_dtb{len(found) or ''}", container)
            if info:
                info["offset_in_container"] = off
                found.append(info)
        return found

    # -------------------------------------------------------------- containers
    def handle_boot(self, img, base: int, size: int, label: str) -> dict:
        boot = rkimg.parse_android_boot(img[base:base + min(size, SCAN_HEAD)])
        assert boot is not None
        d = {"type": "android-boot", "version": boot.version,
             "page_size": boot.page_size, "name": boot.name,
             "cmdline": boot.cmdline, "warnings": boot.warnings, "segments": []}
        self.say(f"    Android boot image v{boot.version}, page_size={boot.page_size}")
        if boot.name:
            self.say(f"      name   : {boot.name}")
        if boot.cmdline:
            self.say(f"      cmdline: {boot.cmdline[:200]}")
        for seg_name, off, seg_size in boot.segments:
            if base + off + seg_size > len(img):
                self.say(f"      {seg_name}: {seg_size} bytes at {off} - TRUNCATED, skipped")
                continue
            seg_path = self.out / f"{label}_{seg_name}.img"
            with open(seg_path, "wb") as f:
                f.write(img[base + off:base + off + seg_size])
            entry = {"name": seg_name, "offset": off, "size": seg_size,
                     "file": str(seg_path.relative_to(self.out))}
            self.say(f"      {seg_name:8s} {human(seg_size):>10s} -> {entry['file']}")
            if seg_name == "kernel":
                m = re.search(rb"Linux version ([^\n\x00]+)", img[base + off:base + off + min(seg_size, SCAN_HEAD)])
                if m:
                    entry["kernel_version"] = m.group(1).decode("utf-8", "replace")
                    self.say(f"      kernel  : {entry['kernel_version']}")
                entry["compression"] = rkimg.identify(img[base + off:base + off + 4])
            if seg_name == "dtb":
                total = rkimg.fdt_totalsize(img, base + off)
                info = self.save_fdt(img, base + off, f"{label}_dtb", "boot.img:dtb")
                if info:
                    d["segments"].append(info)
                    continue
                if total <= 0:
                    self.say("      dtb: no FDT magic - not a device tree")
            d["segments"].append(entry)
        # Rockchip appends resource.img after the boot image
        if boot.trailing_offset and base + boot.trailing_offset < base + size:
            rest = rkimg.identify(img[base + boot.trailing_offset:
                                     base + boot.trailing_offset + 4096])
            if rest != "zeroes":
                self.say(f"      trailing payload at +{boot.trailing_offset}: {rest}")
                d["trailing"] = self.handle_container(img, base + boot.trailing_offset,
                                                      size - boot.trailing_offset,
                                                      f"{label}_trailing", 1)
        return d

    def handle_rsce(self, img, base: int, size: int, label: str) -> dict:
        res = rkimg.parse_rsce(img[base:base + min(size, SCAN_HEAD)], base_offset=base)
        if res is None:
            return {"type": "rockchip-resource", "error": "RSCE header did not validate"}
        self.say(f"    Rockchip resource image ({res.layout} header layout), "
                 f"{len(res.entries)} entries")
        d = {"type": "rockchip-resource", "layout": res.layout, "entries": []}
        for entry in res.entries:
            name = os.path.basename(entry.path) or "resource"
            absolute = entry.offset
            entry_path = self.out / f"{label}_{name}"
            available = max(0, min(entry.size, len(img) - absolute))
            blob = img[absolute:absolute + available]
            with open(entry_path, "wb") as f:
                f.write(blob)
            rec = {"path": entry.path, "offset": absolute, "size": entry.size,
                   "file": str(entry_path.relative_to(self.out)),
                   "complete": available == entry.size}
            # the index carries a SHA-1/SHA-256 of the content: use it
            if rec["complete"] and entry.hash_size in (20, 32):
                algo = "sha1" if entry.hash_size == 20 else "sha256"
                rec["hash_ok"] = (hashlib.new(algo, blob).digest()
                                  == entry.hash[:entry.hash_size])
                if not rec["hash_ok"]:
                    self.say(f"      ** {entry.path}: content hash MISMATCH **")
            self.say(f"      {entry.path:40s} {human(entry.size):>10s} -> {rec['file']}"
                     + ("" if rec["complete"] else "  ** truncated **"))
            if name.endswith(".dtb"):
                info = self.save_fdt(img, absolute, f"{label}_{Path(name).stem}", "resource.img")
                if info:
                    rec["dtb"] = info
            d["entries"].append(rec)
        return d

    def handle_container(self, img, base: int, size: int, label: str, depth: int = 1) -> dict:
        """Identify and fully decompose whatever container starts at `base`."""
        if depth > 3 or base >= len(img):
            return {"type": "unknown"}
        head = img[base:base + min(size, SCAN_HEAD)]
        kind = rkimg.identify(head, size)
        if kind == "android-boot":
            return self.handle_boot(img, base, size, label)
        if kind == "rockchip-resource":
            return self.handle_rsce(img, base, size, label)
        if kind in ("rk-uboot-image", "rk-trust-image", "rk-kernel-image"):
            hdr = rkimg.parse_loader_header(head)
            self.say(f"    Rockchip {kind} ({hdr.name}) version {hdr.version}, "
                     f"data size {human(hdr.size)}")
            return {"type": kind, "name": hdr.name, "version": hdr.version,
                    "data_size": hdr.size}
        if kind == "krnl-image":
            return self.handle_krnl(img, base, size, label, depth)
        if kind == "cpio":
            entries = rkimg.parse_cpio(bytes(img[base:base + min(size, 256 << 20)]))
            if entries is None:
                self.say("    cpio archive did not parse")
                return {"type": "cpio", "error": "parse failed"}
            self.say(f"    cpio archive: {len(entries)} entries")
            for entry in entries[:20]:
                self.say(f"      {entry['size']:>10d}  {entry['name']}")
            if len(entries) > 20:
                self.say(f"      ... {len(entries) - 20} more")
            return {"type": "cpio", "entries": entries}
        if kind == "dtb-only":
            nodes = rkimg.fdt_parse(img, base)
            if nodes is not None and rkimg.fdt_is_fit(nodes):
                return self.handle_fit(img, base, size, label, depth)
            infos = self.scan_fdts(img, base, size, label, label)
            return {"type": "dtb-only", "dtbs": infos}
        if kind in rkimg.FILESYSTEMS:
            self.say(f"    filesystem: {kind}")
            return {"type": "filesystem", "fs": kind}
        if kind == "rockchip-parameter":
            parts, meta = rkimg.parse_rk_parameter(head, len(img))
            self.say(f"    Rockchip parameter partition: {len(parts)} entries")
            for p in parts:
                self.say(f"      {p}")
            return {"type": "rockchip-parameter", "partitions": [p.__dict__ for p in parts],
                    "meta": meta}
        if kind == "zeroes":
            return {"type": "zeroes"}
        self.say(f"    unidentified ({kind})")
        return {"type": "unknown", "kind": kind}

    def handle_fit(self, img, base: int, size: int, label: str, depth: int) -> dict:
        """U-Boot FIT: kernel/ramdisk/DTB payloads live in /images/<name>/data."""
        nodes = rkimg.fdt_parse(img, base)
        if nodes is None:
            return {"type": "fit", "error": "FDT structure block did not parse"}
        desc = rkimg.fdt_prop_str(nodes.get("/", {}), "description") or ""
        self.say(f"    U-Boot FIT image: {desc or '(no description)'}")
        out = {"type": "fit", "description": desc, "subimages": []}
        for path in sorted(nodes):
            if not path.startswith("/images/") or path.count("/") != 2:
                continue
            props = nodes[path]
            name = path.rsplit("/", 1)[1]
            raw = props.get("data")
            if raw is None:
                off = rkimg.fdt_prop_int(props, "data-offset") or \
                    rkimg.fdt_prop_int(props, "data-position")
                length = rkimg.fdt_prop_int(props, "data-size")
                if off is None or length is None:
                    self.say(f"      {name}: external data, not inline - skipped")
                    out["subimages"].append({"name": name, "error": "external data"})
                    continue
                raw = img[base + off:base + off + length]
            compression = rkimg.fdt_prop_str(props, "compression") or "none"
            blob, note = _decompress(bytes(raw), compression)
            sub = {"name": name, "compression": compression,
                   "description": rkimg.fdt_prop_str(props, "description") or "",
                   "size": len(blob)}
            if note:
                sub["error"] = note
            dest = self.out / f"{label}_{name}"
            with open(dest, "wb") as f:
                f.write(blob)
            sub["file"] = str(dest.relative_to(self.out))
            # the FIT carries a hash node per sub-image; it covers the bytes as
            # stored (i.e. still compressed), not the decoded payload
            hash_props = nodes.get(path + "/hash")
            if hash_props and "value" in hash_props:
                algo = rkimg.fdt_prop_str(hash_props, "algo") or "sha256"
                sub["hash_ok"] = (hashlib.new(algo, bytes(raw)).digest()
                                  == hash_props["value"])
                if not sub["hash_ok"]:
                    self.say(f"      ** {name}: FIT hash MISMATCH ({algo}) **")
            self.say(f"      {name:12s} {human(len(blob)):>10s} {compression:6s} -> "
                     f"{sub['file']}" + (f"  ({note})" if note else ""))
            if blob[:4] == struct.pack(">I", rkimg.FDT_MAGIC):
                info = self.save_fdt_bytes(blob, f"{label}_{name}", "fit")
                if info:
                    sub["dtb"] = info
            elif name.startswith("kernel"):
                m = re.search(rb"Linux version ([^\n\x00]+)", blob[:SCAN_HEAD])
                if m:
                    sub["kernel_version"] = m.group(1).decode("utf-8", "replace")
                    self.say(f"        kernel: {sub['kernel_version']}")
            out["subimages"].append(sub)
        return out

    def handle_krnl(self, img, base: int, size: int, label: str, depth: int) -> dict:
        """FriendlyElec mkkrnlimg container: 'KRNL' + u32 size + gzip stream."""
        declared = rkimg.u32(img, base + 4)
        payload = img[base + 8:base + min(size, declared + 8)]
        do = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            blob = do.decompress(bytes(payload))
        except zlib.error as exc:
            self.say(f"    KRNL container: gzip payload failed: {exc}")
            return {"type": "krnl-image", "error": str(exc)}
        self.say(f"    KRNL container: declared {human(declared)}, "
                 f"inflated {human(len(blob))}")
        dest = self.out / f"{label}_inner.bin"
        dest.write_bytes(blob)
        out = {"type": "krnl-image", "declared_size": declared,
               "inflated_size": len(blob), "file": str(dest.relative_to(self.out))}
        if blob:
            out["inner"] = self.handle_container(blob, 0, len(blob),
                                                 f"{label}_inner", depth + 1)
        return out

    # ------------------------------------------------------------------ device
    def handle_device_image(self, path: Path, img, parts, table, mode) -> dict:
        size = len(img)
        self.say(f"  layout: {mode}  size {human(size)}")
        if mode.startswith("gpt"):
            if table.get("crc_ok") is False:
                self.say("  ** GPT header CRC mismatch (damaged table or truncated dump) **")
            if table.get("entries_crc_ok") is False:
                self.say("  ** GPT entry-array CRC mismatch **")
            if table.get("truncated"):
                self.say("  ** GPT entry array not fully contained in this image **")
        if table.get("extended"):
            self.say("  ** MBR extended-partition chain present and NOT followed **")
        if not parts:
            self.say("  no usable partition table - "
                     "for a legacy Rockchip layout dump the `parameter` partition too")
            return {"path": str(path), "size": size, "layout": mode, "partitions": []}

        self.say(f"  {len(parts)} partitions:")
        for p in parts:
            self.say(f"    {p}")

        # Rockchip keeps idbloader/u-boot/trust in the gaps between the table and
        # the first partition; those bytes belong to no partition at all.
        gaps = []
        cursor = 34 * SECTOR
        for p in sorted(parts, key=lambda x: x.start):
            if p.start > cursor:
                gaps.append((cursor, min(p.start - cursor, 64 << 20)))
            cursor = max(cursor, p.end)
        for start, length in gaps:
            hits = rkimg.find_loader_images(img[start:start + length], length)
            for off, label in hits:
                hdr = rkimg.parse_loader_header(img[start + off:start + off + 512])
                extra = (f" ({hdr.name} v{hdr.version})" if hdr else "")
                self.say(f"    raw {label:12s} @0x{start + off:010x}{extra}")
        records = []
        for p in parts:
            if p.start >= size:
                self.say(f"    [{p.name}] lies past end of image - skipped")
                continue
            avail = min(p.size, size - p.start)
            self.say(f"    [{p.name}] analysing {human(avail)}")
            rec = {"name": p.name, "start": p.start, "size": p.size,
                   "index": p.index, "type_guid": p.type_guid}
            slice_ = img[p.start:p.start + min(avail, SCAN_HEAD)]
            rec["kind"] = rkimg.identify(slice_, avail)
            if rec["kind"] == "zeroes":
                self.say(f"    [{p.name}] all zeroes")
            else:
                rec["content"] = self.handle_container(img, p.start, avail, p.name, 1)
                if rec["kind"] not in rkimg.CONTAINERS and rec["kind"] not in rkimg.FILESYSTEMS:
                    extra = self.scan_fdts(img, p.start, avail, p.name, p.name)
                    if extra:
                        rec["content"] = {"type": "fdt-scan", "dtbs": extra}
            if self.carve == "all" or (self.carve == "small" and p.size <= self.carve_limit):
                dest = self.out / f"{p.index:02d}_{p.name}.img"
                with open(dest, "wb") as f:
                    f.write(img[p.start:p.start + avail])
                rec["carved"] = str(dest.relative_to(self.out))
                self.say(f"    [{p.name}] carved -> {rec['carved']}")
            records.append(rec)
        return {"path": str(path), "size": size, "layout": mode,
                "table": {k: v for k, v in table.items() if k != "source"},
                "partitions": records}

    # ------------------------------------------------------------------ driver
    def run_file(self, path: Path) -> dict:
        size = path.stat().st_size
        self.say(f"== {path}  ({human(size)})")
        if size == 0:
            self.say("   empty")
            return {"path": str(path), "size": 0}
        with open(path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as img:
                kind = rkimg.identify(img[:min(size, SCAN_HEAD)], size)
                single = kind in rkimg.CONTAINERS or kind in rkimg.FILESYSTEMS
                if not single:
                    parts, table = rkimg.parse_gpt(img, 0, size)
                    mode = "gpt"
                    if not parts:
                        tail = max(0, size - 2 * SECTOR)
                        parts, backup = rkimg.parse_gpt(img, tail - SECTOR, size)
                        if parts:
                            mode, table = "gpt-backup", backup
                    if not parts:
                        parts, table = rkimg.parse_mbr(img, 0, size)
                        mode = "mbr" if parts else "none"
                    if parts:
                        return self.handle_device_image(path, img, parts, table, mode)
                rec = {"path": str(path), "size": size, "kind": kind}
                self.say(f"  identified as: {kind}")
                rec["content"] = self.handle_container(img, 0, size, path.stem, 0)
                return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("extracted"))
    ap.add_argument("--carve", choices=("all", "small", "none"), default="small",
                    help="carve partitions out of a whole-device image (default: small)")
    ap.add_argument("--carve-limit", type=int, default=256 << 20,
                    help="size limit for --carve small (default 256 MiB)")
    ap.add_argument("--no-dtc", action="store_true", help="do not decompile DTBs")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    ex = Extractor(args.out, carve=args.carve, carve_limit=args.carve_limit,
                   no_dtc=args.no_dtc)

    inputs: list[Path] = []
    for path in args.inputs:
        if path.is_dir():
            inputs.extend(sorted(p for p in path.iterdir() if p.is_file()))
        else:
            inputs.append(path)

    for path in inputs:
        if path.suffix in (".zst", ".gz", ".xz", ".bz2", ".lz4"):
            ex.say(f"!! {path} is compressed; decompress first, e.g. "
                   f"`zstd -d -k {path}`")
            continue
        try:
            ex.manifest["images"].append(ex.run_file(path))
        except Exception as exc:  # keep going: one bad image must not kill the run
            ex.say(f"!! {path}: {type(exc).__name__}: {exc}")

    (args.out / "report.txt").write_text("\n".join(ex.report) + "\n")
    (args.out / "manifest.json").write_text(json.dumps(ex.manifest, indent=2) + "\n")
    print(f"\nreport: {args.out / 'report.txt'}")
    print(f"manifest: {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
