#!/usr/bin/env python3
"""Build a Rockchip-style external FIT image (boot.img) for a U-Boot that predates
the `data-offset` property.

Why not just `mkimage -f x.its -E`? Because Rockchip's U-Boot for RK3568 is based
on U-Boot 2017.09, which looks up FIT payloads through `data-position`, and
because the vendor's own boot.img uses that older spelling together with a plain
`hash` node. Modern mkimage emits `data-offset` + `hash-1`, so this builds the
structure directly and keeps the vendor's conventions.

Layout produced (matching the vendor's boot.img):
    [FIT structure, padded to 0x800][fdt][kernel][resource]
with per-image `data-position`/`data-size`, sha256 `hash` nodes, the Rockchip
`multi` configuration hook, and `rollback-index = 0`. No signature node - the
vendor's own FIT carries an empty one (no value), i.e. their firmware is
unsigned, so an unsigned FIT is what the bootloader already accepts.

    rk-fit.py --kernel Image --dtb board.dtb [--resource resource.img] --out boot.img
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
# The shared Rockchip parser module lives in <repo>/lib, imported by path.
sys.path.insert(0, str(ROOT / "lib"))
import rkimg  # noqa: E402

# dtc is the authority on DTB syntax; take it from $DTC or PATH.  --help does
# not need it, so the check happens after argument parsing.
DTC = os.environ.get("DTC") or shutil.which("dtc")
ALIGN = 0x800          # the vendor's structure occupies 0x800 bytes
DESCRIPTION = "U-Boot FIT source file for arm"


def build_dts(payloads: list[tuple[str, int, int]], config_extra: str = "") -> str:
    """payloads: (name, data_position, data_size) in output order."""
    images = []
    for name, pos, size in payloads:
        if name == "fdt":
            kind = ("flat_dt", 'load = <0x08300000>;')
        elif name == "kernel":
            kind = ("kernel", 'os = "linux";\n\t\t\tload = <0x00280000>;\n'
                             '\t\t\tentry = <0x00280000>;')
        else:
            kind = ("multi", "")
        itype, extra = kind
        images.append(f"""\t\t{name} {{
\t\t\tdescription = "{name}";
\t\t\tdata-position = <0x{pos:x}>;
\t\t\tdata-size = <0x{size:x}>;
\t\t\ttype = "{itype}";
\t\t\tarch = "arm64";
\t\t\tcompression = "none";
\t\t\t{extra}
\t\t\thash {{
\t\t\t\talgo = "sha256";
\t\t\t\tvalue = [@HASH_{name}@];
\t\t\t}};
\t\t}};""")
    return f"""/dts-v1/;

/ {{
\tdescription = "{DESCRIPTION}";
\t#address-cells = <1>;

\timages {{
{chr(10).join(images)}
\t}};

\tconfigurations {{
\t\tdefault = "conf";

\t\tconf {{
\t\t\tdescription = "ZSpace T2";
\t\t\tkernel = "kernel";
\t\t\tfdt = "fdt";
{config_extra}\t\t\trollback-index = <0x0>;
\t\t}};
\t}};
}};
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", type=Path, required=True)
    ap.add_argument("--dtb", type=Path, required=True)
    ap.add_argument("--resource", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if not DTC:
        sys.exit("rk-fit: dtc not found: install device-tree-compiler or set "
                 "$DTC to its path")

    payloads = [("fdt", 0, args.dtb.stat().st_size),
                ("kernel", 0, args.kernel.stat().st_size)]
    blobs = {"fdt": args.dtb.read_bytes(), "kernel": args.kernel.read_bytes()}
    if args.resource:
        payloads.append(("resource", 0, args.resource.stat().st_size))
        blobs["resource"] = args.resource.read_bytes()

    dts = build_dts(payloads, '\t\t\tmulti = "resource";\n' if args.resource else "")

    # pass 1: compile to learn the structure size, so payload offsets can be fixed
    def compile_dts(text: str, out: Path) -> None:
        r = subprocess.run([str(DTC), "-I", "dts", "-O", "dtb", "-o", str(out), "-"],
                           input=text, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"dtc failed:\n{r.stderr}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        probe = tmp / "probe.dtb"
        compile_dts(dts.replace("@HASH_fdt@", "00" * 32).replace("@HASH_kernel@", "00" * 32)
                       .replace("@HASH_resource@", "00" * 32), probe)
        struct_size = max(ALIGN, probe.stat().st_size)
        offset = struct_size
        positions = {}
        for i, (name, _pos, size) in enumerate(payloads):
            # the vendor aligns each payload start to 0x800 except the last one,
            # which packs directly after its predecessor; mirroring that makes
            # the resulting FIT byte-comparable with theirs
            if i < len(payloads) - 1:
                offset = (offset + ALIGN - 1) // ALIGN * ALIGN
            positions[name] = offset
            offset += size
        payloads = [(n, positions[n], s) for n, _p, s in payloads]

        hashes = {n: hashlib.sha256(blobs[n]).digest().hex() for n in blobs}
        text = build_dts(payloads, '\t\t\tmulti = "resource";\n' if args.resource else "")
        for name, digest in hashes.items():
            text = text.replace(f"@HASH_{name}@", digest)
        fit = tmp / "fit.dtb"
        compile_dts(text, fit)
        structure = fit.read_bytes()
        if len(structure) > struct_size:
            sys.exit(f"structure grew to {len(structure)} > {struct_size}; rerun")

        out = bytearray()
        out += structure.ljust(struct_size, b"\0")
        for name, pos, size in sorted(payloads, key=lambda x: x[1]):
            out += b"\0" * (pos - len(out))
            out += blobs[name]
        args.out.write_bytes(bytes(out))

    # verify what we just wrote by reading it back the way the bootloader would
    data = args.out.read_bytes()
    nodes = rkimg.fdt_parse(data, 0)
    if nodes is None:
        sys.exit("verification failed: FIT structure does not parse")
    ok = True
    for name, _pos, _size in payloads:
        props = nodes.get(f"/images/{name}", {})
        pos = rkimg.fdt_prop_int(props, "data-position")
        size = rkimg.fdt_prop_int(props, "data-size")
        blob = data[pos:pos + size]
        recorded = nodes.get(f"/images/{name}/hash", {}).get("value")
        good = recorded == hashlib.sha256(blob).digest()
        ok = ok and good and len(blob) == size
        print(f"  {name:9s} position=0x{pos:x} size={size:,} sha256_ok={good}")
    conf = nodes.get("/configurations/conf", {})
    print(f"  config: {sorted(conf)}  default={rkimg.fdt_prop_str(nodes['/configurations'], 'default')}")
    print(f"  signature node: {'/configurations/conf/signature' in nodes}")
    print(f"wrote {args.out} ({len(data):,} bytes) - verification {'ok' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
