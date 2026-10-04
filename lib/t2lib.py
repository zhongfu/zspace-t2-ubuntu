#!/usr/bin/env python3
"""t2lib.py - shared helpers for the T2 distro drivers.

``rootfs/t2-distro.py`` loads this file by path (importlib; a hyphen-free name
keeps a normal import working too).  It holds only the helpers that driver
actually uses: the command runner, the small file/size utilities, and the
rockusb flash-plan arithmetic.  Everything that was specific to building the
kernel/FIT has moved to the kernel repository.

This file lives at ``<repo>/lib/t2lib.py``.  Resolve every path from its own
location so it works from any clone, whatever the current directory is.
"""

from __future__ import annotations

import hashlib
import platform
import shlex
import subprocess
import sys
import sysconfig
from pathlib import Path

# --------------------------------------------------------------------------
# Fixed facts about this project / board (provenance in comments)
# --------------------------------------------------------------------------
SCRIPTS = Path(__file__).resolve().parent   # <repo>/lib (also holds rkimg.py)
ROOT = SCRIPTS.parent                       # the repository root

RKDEVELOPTOOL = ROOT / "tools" / "rkdeveloptool" / "build" / "rkdeveloptool"


def host_multiarch() -> str:
    """The host's multiarch tuple, e.g. x86_64-linux-gnu or
    aarch64-linux-gnu.  A vendored toolchain keeps its host libraries under
    ``usr/lib/<tuple>``, and that tuple differs by host architecture (the
    target is always arm64; only the host varies)."""
    return (sysconfig.get_config_var("MULTIARCH")
            or f"{platform.machine()}-linux-gnu")


# GPT, parsed from a full vendor flash dump (supports the vendor layout):
ROOTFS_SIZE = 1925152768        # p11 "source_rootfs" = 3,760,064 sectors * 512
FLASH_CHUNK = 8 * 1024 * 1024   # rockusb `wl` chunk used by t2-flash.py


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"t2lib: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def human(n: int) -> str:
    return f"{n:,} B ({n / 2**20:.1f} MiB)"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_size(text: str) -> int:
    """Accept 1925152768, 1836M, 1.8G ... (M/G = MiB/GiB)."""
    if text == "p11":
        return ROOTFS_SIZE
    t = text.strip()
    mult = 1
    if t and t[-1] in "kKmMgG":
        mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[t[-1].lower()]
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        die(f"cannot parse size {text!r}")


class Runner:
    """Prints every command; executes it unless --dry-run."""

    def __init__(self, dry: bool) -> None:
        self.dry = dry

    def run(self, argv, *, env=None, cwd=None, capture=False):
        pretty = " ".join(shlex.quote(str(a)) for a in argv)
        log(f"  $ {pretty}")
        if self.dry:
            return None
        return subprocess.run([str(a) for a in argv], env=env, cwd=cwd,
                              check=True, text=True,
                              capture_output=capture)

    def shell(self, script: str, *, env=None, cwd=None, capture=False):
        log("  $ bash -c " + shlex.quote(script))
        if self.dry:
            return None
        return subprocess.run(["bash", "-c", script], env=env, cwd=cwd,
                              check=True, text=True, capture_output=capture)


# --------------------------------------------------------------------------
# flash plan (computed, never executed unless --flash)
# --------------------------------------------------------------------------
def flash_plan(size: int, lba: int, part_size: int) -> dict:
    """rockusb `wl` chunk plan.  The final chunk is naturally short (the file
    ends there), so the writes never cross the partition boundary."""
    if size > part_size:
        die(f"image {human(size)} does not fit the partition {human(part_size)}")
    n = (size + FLASH_CHUNK - 1) // FLASH_CHUNK
    last = size - (n - 1) * FLASH_CHUNK
    end = lba + (size + 511) // 512
    per = FLASH_CHUNK // 512
    return {"lba_start": hex(lba), "chunk_size": FLASH_CHUNK, "chunks": n,
            "chunk_lba_stride": hex(per),
            "lba_last_chunk": hex(lba + (n - 1) * per),
            "last_chunk_bytes": last, "end_lba_exclusive": hex(end),
            "bytes": size,
            "cmd": f"{RKDEVELOPTOOL} wl <lba_start + i*{hex(per)}> "
                   f"chunkNN.bin   # {n} chunks, then 'rd'"}


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def artifact(path: Path, name: str, **extra) -> dict:
    d = {"name": name, "path": str(path), "size": path.stat().st_size,
         "sha256": sha256_file(path)}
    d.update(extra)
    return d
