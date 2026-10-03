#!/usr/bin/env python3
"""Assemble a flashable **whole-disk** ZSpace T2 image (SD card, or an eMMC
payload written with `dd`/`rkdeveloptool`).

The distro builder produces two pieces - `rootfs.ext4` and a kernel `FIT` - and
`scripts/t2-flash.py` writes them into an already-partitioned eMMC (p6 and p3).
A card, by contrast, has no partitions at all, so this script authors the GPT
and lays the pieces down at the same offsets the vendor loader expects on eMMC:

  LBA 0x40      idbloader.img          (unpartitioned, RKNS; + guarded copies)
  LBA 0x4000    p1 `uboot` 4 MiB       u-boot.itb (vendor U-Boot FIT)
  LBA 0x6000    p2 `misc`  4 MiB       (empty - the vendor bootloader's scratch)
  LBA 0x8000    p3 `boot`  64 MiB      the mainline kernel FIT   <- boots this
  LBA 0x28000   p4 `recovery` 32 MiB   (empty, kept so partition *numbers* match)
  LBA 0x38000   p5 `backup`   32 MiB   (empty, ditto)
  LBA 0x48000   p6 `rootfs`           rootfs.ext4 (label zspace-rootfs)

Why mirror the vendor geometry instead of inventing one: the vendor SPL/U-Boot
reads the loader from LBA 0x40 and its FIT from p1, and the vendor environment
booting the *boot* partition - all of it measured on the device.  Keeping the
same names, type GUIDs and PARTUUIDs (the
device's own GPT, embedded in `VENDOR_TABLE` below) means the card is laid out
exactly like the eMMC, so the loader that already works on eMMC has the same
numbers to find.

p7..p11 (`oem`, `userdata`, `log`, `source_kernel`, `source_rootfs`) are
vendor-only and are deliberately dropped: the card has no use for a 12 GiB
userdata or a copy of the vendor recovery kernel.  p4/p5 are kept *empty*
because dropping them would renumber `rootfs`, and a bootloader that refers to
it by number would then write the wrong partition.

Two further partitions can be appended after `rootfs` for the card-driven
installer:

  * a `config` FAT (`--config`/`--boot-dir`, `--config-size`, `--config-label`)
    carrying `/t2-config.txt` merged with the boot tree *as files* copied to
    the FAT root: `/Image`, the DTB, `/extlinux/extlinux.conf` (the card
    descriptor), `/extlinux/t2-emmc.conf` (the eMMC descriptor), `/uboot.env`
    and `/Image.old`, plus `/u-boot.itb` and `/idbloader.img` (the installer
    writes those to the new p1/SPL).  256 MiB (`--config-size 256M`) holds the
    kernel, its fallback and the loaders with room to spare - the old 16 MiB
    default cannot;
  * one `payload` ext4 partition (`--payload-dir`, `--payload-size`,
    `T2-FLASH`) whose directory is copied to the filesystem root, so the
    rootfs image the installer streams is `/rootfs.ext4.zst`.  It carries
    *only* the rootfs image (plus whatever else the caller's directory holds).

The file is sparse: payloads are written with seek+skip-zero-chunk copying, so
the untouched tail of the rootfs partition stays holes - that is what makes the
image cheap to store and fast to flash (`dd conv=sparse` on the flashing side).

Everything the script writes is verified by reading the finished image back with
`lib/rkimg.py`: the GPT parses with valid CRCs and the expected table, and each
payload region hashes to its input file.

Inputs default to `<repo>/build/out/`; outputs are normally written there too.
Run from the repository root or from `images/` - the script resolves its paths
from its own location, never the working directory.

Usage:
    images/t2-image.py --out build/out/t2-base-sd.img
    images/t2-image.py --rootfs none --size 3.5G --out /tmp/loader-only.img
    # the installer card: FAT (config + boot tree files + both loaders) plus one
    # ext4 payload partition holding /rootfs.ext4.zst
    images/t2-image.py --rootfs none --size auto --out build/out/installer.img \
        --config t2-config.txt --config-size 256M \
        --boot-dir card-boot-tree --payload-dir payload-tree
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
# The shared Rockchip parser module lives in <repo>/lib (imported by path, not
# installed): images/, tools/ and rootfs/ all use the same copy.
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
import rkimg  # noqa: E402


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"t2-image: error: {msg}", file=sys.stderr, flush=True)
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
    t = text.strip()
    mult = 1
    if t and t[-1] in "kKmMgG":
        mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[t[-1].lower()]
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        die(f"cannot parse size {text!r}")


SECTOR = rkimg.SECTOR
# The device's own GPT, dumped with `dd if=/dev/mmcblk0 bs=512 count=34` and
# embedded here (rather than read from a vendor dump file at run time):
# ground truth for partition names, type GUIDs, PARTUUIDs and start LBAs.
VENDOR_TABLE = [
    {"index": 1, "name": "uboot", "type": "f2690000-0000-4c72-8000-62e0000025d6",
     "partuuid": "3a100000-0000-4701-8000-4de500006d2f",
     "start_lba": 16384, "last_lba": 24575, "size": 8192},
    {"index": 2, "name": "misc", "type": "ba240000-0000-4410-8000-56090000642c",
     "partuuid": "14740000-0000-4551-8000-029000005e5d",
     "start_lba": 24576, "last_lba": 32767, "size": 8192},
    {"index": 3, "name": "boot", "type": "28300000-0000-4a6d-8000-1ffa00000b93",
     "partuuid": "18380000-0000-4550-8000-1207000049df",
     "start_lba": 32768, "last_lba": 163839, "size": 131072},
    {"index": 4, "name": "recovery", "type": "436c0000-0000-467b-8000-2bba00006f92",
     "partuuid": "4c7c0000-0000-4f53-8000-70520000243d",
     "start_lba": 163840, "last_lba": 229375, "size": 65536},
    {"index": 5, "name": "backup", "type": "143a0000-0000-4171-8000-7050000028b2",
     "partuuid": "ba660000-0000-4c74-8000-117700002a00",
     "start_lba": 229376, "last_lba": 294911, "size": 65536},
    {"index": 6, "name": "rootfs", "type": "a1010000-0000-424c-8000-64c700001379",
     "partuuid": "614e0000-0000-4b53-8000-1d28000054a9",
     "start_lba": 294912, "last_lba": 29655039, "size": 29360128},
    {"index": 7, "name": "oem", "type": "f42d0000-0000-4149-8000-032400004f71",
     "partuuid": "62500000-0000-4c48-8000-0874000073d2",
     "start_lba": 29655040, "last_lba": 29917183, "size": 262144},
    {"index": 8, "name": "userdata", "type": "152b0000-0000-476d-8000-4d7100004022",
     "partuuid": "ac2e0000-0000-4001-8000-5c5c00007ef2",
     "start_lba": 29917184, "last_lba": 55083007, "size": 25165824},
    {"index": 9, "name": "log", "type": "0a6d0000-0000-4a7e-8000-45b600000f13",
     "partuuid": "506c0000-0000-4664-8000-723f00004496",
     "start_lba": 55083008, "last_lba": 57180159, "size": 2097152},
    {"index": 10, "name": "source_kernel",
     "type": "dd6c0000-0000-4507-8000-74f9000028cc",
     "partuuid": "54420000-0000-475f-8000-542500005346",
     "start_lba": 57180160, "last_lba": 57311231, "size": 131072},
    {"index": 11, "name": "source_rootfs",
     "type": "96210000-0000-4d58-8000-644400003ad5",
     "partuuid": "c26e0000-0000-4922-8000-0f1700007e8a",
     "start_lba": 57311232, "last_lba": 61071295, "size": 3760064},
]
# The vendor GPT's disk GUID (the same in every copy of the layout): reused so
# the card is a stand-in for the eMMC, not a foreign disk.
VENDOR_DISK_GUID = "81700000-0000-4350-8000-07fc000044ce"
# Which vendor partitions the card carries, and what goes in each.
CARRY = ("uboot", "misc", "boot", "recovery", "backup", "rootfs")
# Out-of-band config partition (P3): a FAT filesystem the image reads on boot.
# Uppercase label on purpose - the partition is meant to be written from any OS,
# and lowercase VFAT labels are documented to misbehave on some of them.
CONFIG_NAME = "config"
CONFIG_LABEL = "T2-CONFIG"
CONFIG_FILE = "t2-config.txt"
CONFIG_TYPE_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"  # EFI System
CONFIG_SIZE = 16 << 20
# Deterministic (uuid5 of a fixed name), so rebuilding gives the same table.
CONFIG_PARTUUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "zspace-t2-config"))
# Payload partition (P8): the card-driven installer's second partition.  It
# carries the rootfs image the installer streams into the eMMC ("the payload
# carries only the rootfs image") plus anything else the caller's directory
# holds.  ext4, not FAT: a multi-GB file would hit FAT's 4 GiB - 1 limit.  The
# directory is copied to the filesystem root, so `/rootfs.ext4.zst` sits where
# the installer's `flash.rootfs` names it.
PAYLOAD_NAME = "flash"
PAYLOAD_LABEL = "T2-FLASH"
PAYLOAD_TYPE_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"  # Linux filesystem
PAYLOAD_PARTUUID = str(uuid.uuid5(uuid.NAMESPACE_URL, "zspace-t2-flash"))
# ext4 needs more than the payload: a 16 MiB journal (mke2fs reserves 4096
# blocks of it whatever -m says), the inode table (1 inode per 16 KiB), block
# group overhead.  A fixed 16 MiB is not enough - it failed on a 250 MiB
# payload 2026-10-01 ("Could not allocate block in ext2 filesystem") - so the
# auto size is the payload plus 32 MiB plus 5%.
PAYLOAD_SLACK = 32 << 20        # ext4 journal, inode tables, block groups
PAYLOAD_SLACK_FRACTION = 20     # ... plus 1/20 of the directory contents
PAYLOAD_BLOCK = 4096            # block size; mke2fs's size argument is in these
# Guarded loader copies (the vendor dump shows the same 512 KiB block five
# times; Rockchip documents only LBA 0x40, so this is cheap insurance).
LOADER_LBAS = (0x40, 0x440, 0x840, 0xc40, 0x1040)
# Build inputs, all produced by the sibling subtrees into <repo>/build/out.
DEFAULTS = {
    "idbloader": ROOT / "build/out/idbloader.img",
    "uboot": ROOT / "build/out/u-boot.itb",
    "fit": ROOT / "build/out/t2-mainline-boot.img",
    "rootfs": ROOT / "build/out/rootfs.ext4",
}


def vendor_table() -> list:
    """The vendor partition table (names, type GUIDs, PARTUUIDs, LBAs)."""
    return [dict(r) for r in VENDOR_TABLE]


def plan(rows: list, rootfs: Path | None, total: int,
         config_sectors: int = 0, payload_sectors: int = 0) -> list:
    """Partition rows to create, with `rootfs` sized to the image (and room
    reserved at the end for the optional `config` FAT and `payload` ext4
    partitions)."""
    carried = [r for r in rows if r["name"] in CARRY]
    if not carried:
        die("the vendor GPT has none of " + ", ".join(CARRY))
    out = []
    for r in carried:
        if r["name"] != "rootfs":
            out.append(r)
            continue
        start = r["start_lba"]
        # fill the image: everything from the rootfs start up to the backup GPT,
        # minus the space the config partition needs after it
        room = ((total // SECTOR) - start - 34 - config_sectors
                - payload_sectors)
        if room <= 0:
            die(f"image is too small: rootfs starts at LBA {start:#x}, but the "
                f"image is only {total // SECTOR} sectors")
        need = ((rootfs.stat().st_size + SECTOR - 1) // SECTOR) if rootfs else 0
        if need and need > room:
            die(f"rootfs ({human(rootfs.stat().st_size)}) does not fit: the "
                f"partition would be {human(room * SECTOR)}")
        rootfs_row = dict(r, size=room)
        out.append(rootfs_row)
        if config_sectors:
            # new partition, so a fresh number (vendor p7..p11 are dropped) and
            # a fresh GUID: nothing on the device refers to it by either, and
            # the image finds it by LABEL (blkid -t LABEL=...)
            out.append({
                "index": max(x["index"] for x in carried) + 1,
                "name": CONFIG_NAME,
                "type": CONFIG_TYPE_GUID,
                "partuuid": CONFIG_PARTUUID,
                "start_lba": rootfs_row["start_lba"] + rootfs_row["size"],
                "size": config_sectors,
            })
        if payload_sectors:
            prev = out[-1]
            out.append({
                "index": prev["index"] + 1,
                "name": PAYLOAD_NAME,
                "type": PAYLOAD_TYPE_GUID,
                "partuuid": PAYLOAD_PARTUUID,
                "start_lba": prev["start_lba"] + prev["size"],
                "size": payload_sectors,
            })
    return out


def build_config_fat(src: Path | None, label: str, size_bytes: int, out: Path,
                     boot_dir: Path | None = None) -> Path:
    """A FAT filesystem of `size_bytes`, holding `src` as `/t2-config.txt` and,
    when given, the tree `boot_dir` at its root.

    mtools writes the files, so the partition's contents are produced by an
    implementation that understands VFAT rather than by hand-rolled bytes - and
    the result is verified by reading it back with the same tools.

    `boot_dir` is what makes an SD card bootable by *mainline* U-Boot: its
    bootstd scans FAT partitions for `/extlinux/extlinux.conf`, which the raw
    FIT at LBA 0x8000 (the vendor layout this image mirrors) cannot provide.
    """
    out.unlink(missing_ok=True)
    subprocess.run(["truncate", "-s", str(size_bytes), str(out)], check=True)
    r = subprocess.run(["mkfs.vfat", "-n", label, str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"mkfs.vfat failed:\n{r.stdout}{r.stderr}")
    if src is not None:
        r = subprocess.run(["mcopy", "-i", str(out), str(src),
                            f"::/{CONFIG_FILE}"], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"mcopy {src} failed:\n{r.stdout}{r.stderr}")
    if boot_dir is not None:
        for entry in sorted(boot_dir.iterdir()):
            r = subprocess.run(["mcopy", "-s", "-i", str(out), str(entry), "::/"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                die(f"mcopy {entry} failed:\n{r.stdout}{r.stderr}")
    return out


def build_payload_ext4(src: Path, label: str, size_bytes: int, out: Path) -> Path:
    """An ext4 filesystem of `size_bytes` holding the directory `src` at its root.

    mke2fs with `-d` writes the tree without needing root (the ownership it
    finds on the host is irrelevant: the installer reads this partition
    read-only, as root, and only ever streams files out).  The caller's
    directory is copied verbatim to `/`, so `/rootfs.ext4.zst` sits where the
    installer's `flash.rootfs` names it.
    """
    stage = Path(tempfile.mkdtemp(prefix="t2-payload-"))
    # mke2fs takes its size argument in *blocks of the block size*, not bytes:
    # handing it a byte count is how a 17 MiB partition became 68 GiB here
    # (measured 2026-10-01).  Round down to whole blocks.
    blocks = size_bytes // PAYLOAD_BLOCK
    if blocks < 1:
        die(f"{label} partition is smaller than one block")
    try:
        shutil.copytree(src, stage, dirs_exist_ok=True)
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F",
                        "-b", str(PAYLOAD_BLOCK), "-m", "0", "-L", label,
                        "-d", str(stage), str(out), str(blocks)], check=True)
    except subprocess.CalledProcessError as e:
        die(f"mke2fs failed for the {label} partition: {e}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return out


def dir_file_bytes(path: Path) -> int:
    """Total size of the regular files under `path`, recursively."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def debugfs_file_sha(path: Path, name: str) -> str:
    """sha256 of a file inside an ext4 image, read back through debugfs."""
    r = subprocess.run(["debugfs", "-R", f"cat /{name}", str(path)],
                       capture_output=True, check=False)
    if r.returncode != 0:
        return "debugfs-failed:" + r.stderr.decode(errors="replace")[:60]
    return hashlib.sha256(r.stdout).hexdigest()


def probe_fat_label(path: Path) -> str:
    r = subprocess.run(["blkid", "-p", "-o", "value", "-s", "LABEL", str(path)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def fat_file_text(path: Path, member: str = CONFIG_FILE) -> bytes:
    r = subprocess.run(["mtype", "-i", str(path), f"::/{member}"],
                       capture_output=True)
    return r.stdout


def _guid_bytes(text: str) -> bytes:
    """Encode a GUID the way GPT stores it (first three fields little-endian)."""
    a, b, c, d, e = text.split("-")
    return (struct.pack("<IHH", int(a, 16), int(b, 16), int(c, 16))
            + bytes.fromhex(d) + bytes.fromhex(e))


def build_gpt(img: Path, parts: list, total: int) -> None:
    """Write the primary and backup GPT straight into the image.

    Not sgdisk: it aligns partition starts to 1 MiB whatever `-a` says, which
    moved the `config` partition from the LBA the image was sized for (measured
    2026-09-30: asked for 0x5bfde, got 0x5c000, shifting the embedded FAT by 34
    bytes).  Every LBA here is explicit, so the table is written by hand - and
    it keeps the vendor's *disk* GUID and, per partition, the vendor's type GUID,
    PARTUUID and name.
    """
    last = total // SECTOR - 1
    entries = bytearray(128 * 128)
    for p in parts:
        e = bytearray(128)
        e[0:16] = _guid_bytes(p["type"])
        e[16:32] = _guid_bytes(p["partuuid"])
        e[32:40] = struct.pack("<Q", p["start_lba"])
        e[40:48] = struct.pack("<Q", p["start_lba"] + p["size"] - 1)
        name = p["name"].encode("utf-16-le")[:70]
        e[56:56 + len(name)] = name
        off = (p["index"] - 1) * 128
        entries[off:off + 128] = e
    entries_crc = zlib.crc32(bytes(entries)) & 0xFFFFFFFF

    vendor_disk_guid = _guid_bytes(VENDOR_DISK_GUID)

    def header(current: int, other: int, entries_lba: int) -> bytes:
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        h[8:12] = struct.pack("<I", 0x00010000)   # revision 1.0
        h[12:16] = struct.pack("<I", 92)          # header size
        h[24:32] = struct.pack("<Q", current)     # my LBA
        h[32:40] = struct.pack("<Q", other)       # alternate LBA
        h[40:48] = struct.pack("<Q", 34)          # first usable
        h[48:56] = struct.pack("<Q", last - 33)   # last usable
        h[56:72] = vendor_disk_guid
        h[72:80] = struct.pack("<Q", entries_lba)
        h[80:84] = struct.pack("<I", 128)         # entries
        h[84:88] = struct.pack("<I", 128)         # entry size
        h[88:92] = struct.pack("<I", entries_crc)
        h[16:20] = struct.pack("<I", zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h)

    # protective MBR: one entry, type 0xEE, spanning the whole image
    mbr = bytearray(SECTOR)
    mbr[446] = 0x00                                           # not bootable
    mbr[450] = 0xEE                                           # GPT protective type
    mbr[454:458] = struct.pack("<I", 1)                       # first LBA
    mbr[458:462] = struct.pack("<I", min(last, 0xFFFFFFFF))   # sector count
    mbr[510:512] = b"\x55\xaa"

    with img.open("r+b") as fh:
        fh.seek(0)
        fh.write(bytes(mbr) + header(1, last, 2).ljust(SECTOR, b"\0")
                 + bytes(entries))
        fh.seek((last - 32) * SECTOR)
        fh.write(bytes(entries) + header(last, 1, last - 32))


def write_sparse(dst: Path, offset: int, src: Path) -> int:
    """Copy `src` at `offset`, skipping all-zero chunks so the file stays sparse."""
    chunk = 4 << 20
    total = 0
    with dst.open("r+b") as fh, src.open("rb") as f:
        fh.seek(offset)
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            if buf.strip(b"\0"):
                fh.write(buf)
            else:
                fh.seek(len(buf), 1)
            total += len(buf)
    return total


def region_sha(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        fh.seek(offset)
        left = size
        while left:
            buf = fh.read(min(4 << 20, left))
            if not buf:
                die(f"{path}: short read inside a payload region")
            h.update(buf)
            left -= len(buf)
    return h.hexdigest()


def verify(img: Path, parts: list, payloads: list, config=None,
           payload=None) -> list:
    """Read the finished image back: GPT table + every payload region.

    `config`, when given, is `(label, source_file, boot_dir)`: the config
    partition is read back through blkid/mtools rather than by trusting the
    bytes that were written into it.  `payload` is `(label, source_dir)`: the
    ext4 payload partition, read back through debugfs.
    """
    checks = []
    with img.open("rb") as fh:
        head = fh.read(34 * SECTOR)
    got, info = rkimg.parse_gpt(head, 0, None)
    checks.append(("GPT CRCs valid", bool(info.get("crc_ok")),
                   f"header_crc_ok={info.get('crc_ok')} "
                   f"entries_crc_ok={info.get('entries_crc_ok')}"))
    # the protective MBR: some fdisk/lsblk/Windows paths read it before the GPT
    with img.open("rb") as fh:
        fh.seek(0)
        mbr = fh.read(SECTOR)
    mbr_ok = (mbr[450] == 0xEE and mbr[510:512] == b"\x55\xaa"
              and struct.unpack_from("<I", mbr, 454)[0] == 1
              and struct.unpack_from("<I", mbr, 458)[0] == min(
                  img.stat().st_size // SECTOR - 1, 0xFFFFFFFF))
    checks.append(("protective MBR covers the image", mbr_ok,
                   f"type={mbr[450]:#04x} sig={bytes(mbr[510:512])!r} "
                   f"sectors={struct.unpack_from('<I', mbr, 458)[0]}"))
    # the backup header at the end of the image must be valid too: a card whose
    # backup GPT is junk loses its table the moment anything rewrites LBA 0
    with img.open("rb") as fh:
        fh.seek(img.stat().st_size - SECTOR)
        bh = bytearray(fh.read(SECTOR)[:92])
    stored = struct.unpack_from("<I", bh, 16)[0]
    blank = bytearray(bh)
    blank[16:20] = b"\0" * 4
    backup_ok = (bh[0:8] == b"EFI PART"
                 and zlib.crc32(bytes(blank)) & 0xFFFFFFFF == stored)
    checks.append(("backup GPT header CRC valid", backup_ok,
                   f"signature={bytes(bh[0:8])!r} stored_crc={stored:#010x}"))
    got_rows = {p.name: p for p in got}
    expect = {p["name"]: p for p in parts}
    checks.append((f"GPT carries {len(expect)} partitions: "
                   + ", ".join(sorted(expect)), set(got_rows) == set(expect),
                   f"image has {', '.join(sorted(got_rows)) or 'none'}"))
    for name, want in expect.items():
        have = got_rows.get(name)
        ok = have and have.start == want["start_lba"] * SECTOR \
            and have.size == want["size"] * SECTOR
        checks.append((f"{name} at LBA {want['start_lba']:#x} "
                       f"({human(want['size'] * SECTOR)})", bool(ok),
                       f"got {have}" if have else "missing"))
    for label, src, offset in payloads:
        if src is None:
            continue
        want = sha256_file(src)
        got_sha = region_sha(img, offset, src.stat().st_size)
        checks.append((f"{label} == {src.name} @0x{offset:x}",
                       want == got_sha,
                       f"{want[:16]} vs {got_sha[:16]}"))
    if payload:
        label, src_dir = payload
        with tempfile.NamedTemporaryFile(suffix=".ext4", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            with img.open("rb") as fh:
                fh.seek(got_rows[PAYLOAD_NAME].start)
                tmp.write_bytes(fh.read(got_rows[PAYLOAD_NAME].size))
            checks.append((f"payload partition is ext4 labelled {label}",
                           probe_fat_label(tmp) == label,
                           f"blkid says {probe_fat_label(tmp)!r}"))
            for f in sorted(src_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(src_dir).as_posix()
                want = sha256_file(f)
                got_sha = debugfs_file_sha(tmp, rel)
                checks.append((f"/{rel} == {f.name} inside that ext4",
                               want == got_sha, f"{want[:16]} vs {got_sha[:16]}"))
        finally:
            tmp.unlink(missing_ok=True)
    if config:
        label, src, boot_dir = config
        with tempfile.NamedTemporaryFile(suffix=".vfat", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            with img.open("rb") as fh:
                fh.seek(got_rows[CONFIG_NAME].start)
                tmp.write_bytes(fh.read(got_rows[CONFIG_NAME].size))
            checks.append((f"config partition is VFAT labelled {label}",
                           probe_fat_label(tmp) == label,
                           f"blkid says {probe_fat_label(tmp)!r}"))
            if src is not None:
                want = src.read_bytes()
                got_text = fat_file_text(tmp)
                checks.append((f"/{CONFIG_FILE} readable from the embedded FAT",
                               got_text.strip() == want.strip(),
                               f"{len(got_text)} bytes read back"))
            if boot_dir is not None:
                for f in sorted(boot_dir.rglob("*")):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(boot_dir).as_posix()
                    got = fat_file_text(tmp, rel)
                    checks.append(
                        (f"/{rel} readable from the embedded FAT",
                         got == f.read_bytes(),
                         f"{len(got)} of {f.stat().st_size} bytes read back"))
        finally:
            tmp.unlink(missing_ok=True)
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(
        description="assemble a whole-disk T2 image (GPT + vendor loader + FIT "
                    "+ rootfs) at the vendor eMMC offsets")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--idbloader", type=Path, default=DEFAULTS["idbloader"])
    ap.add_argument("--uboot", type=Path, default=DEFAULTS["uboot"])
    ap.add_argument("--fit", type=Path, default=DEFAULTS["fit"])
    ap.add_argument("--rootfs", default=str(DEFAULTS["rootfs"]),
                    help="rootfs.ext4, or 'none' for a loader+FIT-only card "
                         "(what fits on a 4 GB card)")
    ap.add_argument("--size", default="auto",
                    help="image size (e.g. 3.6GiB for a real card), or 'auto' "
                         "= exactly what the payloads need")
    ap.add_argument("--skip-loaders", action="store_true",
                    help="do not write idbloader/u-boot.itb (E1 variant 2: only "
                         "the FIT on the card, the eMMC loader does the loading)")
    ap.add_argument("--no-idbloader", action="store_true",
                    help="write u-boot.itb but not idbloader: the BootROM falls "
                         "back to the eMMC SPL, which then loads U-Boot from the "
                         "card -- the loader-acceptance test")
    ap.add_argument("--config", type=Path,
                    help=f"file to carry on the {CONFIG_LABEL} FAT partition as "
                         f"/{CONFIG_FILE} (the out-of-band config the image "
                         f"reads on boot")
    ap.add_argument("--boot-dir", type=Path,
                    help="directory copied to the root of the same FAT "
                         "partition (the boot tree as files: Image, dtb, "
                         "extlinux/, uboot.env, Image.old - and, for an install "
                         "card, u-boot.itb + idbloader.img that the installer "
                         "writes to the new p1/SPL): what makes the card "
                         "bootable by mainline U-Boot's bootstd")
    ap.add_argument("--config-size", default="16M",
                    help=f"size of that partition (default {human(CONFIG_SIZE)}; "
                         f"an install card carrying the kernel, its fallback and "
                         f"both loader files needs --config-size 256M)")
    ap.add_argument("--config-label", default=CONFIG_LABEL,
                    help=f"VFAT label of that partition (default {CONFIG_LABEL})")
    ap.add_argument("--payload-dir", type=Path,
                    help=f"directory copied to the root of a {PAYLOAD_LABEL} "
                         f"ext4 partition: the payload the installer reads off "
                         f"the card (the rootfs image as /rootfs.ext4.zst)")
    ap.add_argument("--payload-size", default="auto",
                    help=f"size of that partition (default: the directory "
                         f"contents plus {human(PAYLOAD_SLACK)})")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rootfs = None if args.rootfs == "none" else Path(args.rootfs)
    if rootfs and not rootfs.is_file():
        die(f"--rootfs {rootfs} does not exist")
    for p in (args.fit, args.uboot):
        if not p.is_file():
            die(f"{p} does not exist")
    if not args.skip_loaders and not args.no_idbloader and not args.idbloader.is_file():
        die(f"{args.idbloader} does not exist")
    config = args.config
    if config and not config.is_file():
        die(f"--config {config} does not exist")
    boot_dir = args.boot_dir
    if boot_dir and not boot_dir.is_dir():
        die(f"--boot-dir {boot_dir} is not a directory")
    wanted = bool(config or boot_dir)
    config_bytes = parse_size(args.config_size) if wanted else 0
    config_sectors = (config_bytes + SECTOR - 1) // SECTOR if wanted else 0
    payload_dir = args.payload_dir
    if payload_dir and not payload_dir.is_dir():
        die(f"--payload-dir {payload_dir} is not a directory")
    payload_bytes = 0
    if payload_dir:
        contents = dir_file_bytes(payload_dir)
        if args.payload_size != "auto":
            payload_bytes = parse_size(args.payload_size)
        else:
            payload_bytes = (contents + PAYLOAD_SLACK
                             + contents // PAYLOAD_SLACK_FRACTION)
        if payload_bytes < contents:
            die(f"--payload-size {human(payload_bytes)} is smaller than "
                f"the {human(contents)} of {payload_dir}")
    payload_sectors = ((payload_bytes + SECTOR - 1) // SECTOR) if payload_dir else 0

    rows = vendor_table()
    p6 = next(r for r in rows if r["name"] == "rootfs")
    need = p6["start_lba"] * SECTOR + (rootfs.stat().st_size if rootfs else 0)
    if wanted:
        need += config_bytes
    need += payload_bytes if payload_dir else 0
    total = need + (1 << 20) if args.size == "auto" else parse_size(args.size)
    if total < need:
        die(f"--size {human(total)} cannot hold the payloads "
            f"({human(need)} incl. a {human(p6['start_lba'] * SECTOR)} "
            f"partition offset)")
    total = (total + (1 << 20) - 1) // (1 << 20) * (1 << 20)

    parts = plan(rows, rootfs, total, config_sectors, payload_sectors)
    log(f"-- plan: {human(total)} image, {len(parts)} GPT entries --")
    for p in parts:
        log(f"  {p['name']:12s} LBA {p['start_lba']:#08x} "
            f"{human(p['size'] * SECTOR):>10}  type {p['type']}")

    payloads = []
    if not args.skip_loaders:
        if not args.no_idbloader:
            for lba in LOADER_LBAS:
                payloads.append((f"idbloader@{lba:#x}", args.idbloader, lba * SECTOR))
        payloads.append(("u-boot.itb", args.uboot, 0x4000 * SECTOR))
    payloads.append(("kernel FIT", args.fit, 0x8000 * SECTOR))
    if rootfs:
        payloads.append((rootfs.name, rootfs, p6["start_lba"] * SECTOR))

    fat = None
    config_row = next((p for p in parts if p["name"] == CONFIG_NAME), None)
    if config_row:
        fat = Path(tempfile.mkdtemp(prefix="t2-image-")) / f"{CONFIG_NAME}.vfat"
        build_config_fat(config, args.config_label,
                         config_row["size"] * SECTOR, fat, boot_dir)
        payloads.append((f"{args.config_label} FAT partition", fat,
                         config_row["start_lba"] * SECTOR))

    payloadfs = None
    payload_row = next((p for p in parts if p["name"] == PAYLOAD_NAME), None)
    if payload_row:
        payloadfs = (Path(tempfile.mkdtemp(prefix="t2-image-"))
                     / f"{PAYLOAD_NAME}.ext4")
        build_payload_ext4(payload_dir, PAYLOAD_LABEL,
                           payload_row["size"] * SECTOR, payloadfs)
        payloads.append((f"{PAYLOAD_LABEL} ext4 partition", payloadfs,
                         payload_row["start_lba"] * SECTOR))

    if args.dry_run:
        for label, src, off in payloads:
            log(f"  [dry] {label} <- {src} @0x{off:x}")
        if fat:
            shutil.rmtree(fat.parent, ignore_errors=True)
        if payloadfs:
            shutil.rmtree(payloadfs.parent, ignore_errors=True)
        return 0

    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            args.out.unlink()
        subprocess.run(["truncate", "-s", str(total), str(args.out)], check=True)
        build_gpt(args.out, parts, total)
        for label, src, off in payloads:
            n = write_sparse(args.out, off, src)
            log(f"  wrote {label:22s} {human(n):>10} @ LBA {off // SECTOR:#x}")

        log("-- verify (read back) --")
        checks = verify(args.out, parts, payloads,
                        config=(args.config_label, config, boot_dir)
                        if fat else None,
                        payload=(PAYLOAD_LABEL, payload_dir) if payloadfs else None)
        bad = [c for c in checks if not c[1]]
        for name, ok, detail in checks:
            log(f"  [{'ok' if ok else 'FAIL'}] {name}  ({detail})")
        st = args.out.stat()
        written = sum(src.stat().st_size for _, src, _ in payloads)
        log(f"  {args.out}: {human(st.st_size)} apparent, "
            f"{human(written)} of payloads written (the rest is holes)")
        if bad:
            die(f"{len(bad)} check(s) failed")
        log(f"all {len(checks)} checks passed")
    finally:
        if fat:
            shutil.rmtree(fat.parent, ignore_errors=True)
        if payloadfs:
            shutil.rmtree(payloadfs.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
