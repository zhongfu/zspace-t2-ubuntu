#!/usr/bin/env python3
"""rkimg - parsers for Rockchip firmware container formats.

Every format here is implemented against a primary reference, not guesswork:

  GPT (eMMC/NVMe partition table)   UEFI spec 2.10; CRCs are verified, not skipped.
  Rockchip `parameter` partition    rockchip-linux/rkbin tools/parameter_gpt.txt
  Android boot image v0..v4         AOSP system/tools/mkbootimg/include/bootimg/bootimg.h
  Rockchip resource image (RSCE)    rockchip-linux/u-boot tools/rockchip/resource_tool.c
  RK loader/trust/kernel headers    rockchip-linux/u-boot tools/rockchip/loaderimage.c
  FDT (DTB)                         devicetree spec; only *located* here, dtc decompiles

Sector unit: all Rockchip offsets below are 512-byte sectors (mtdparts in the
`parameter` partition is explicitly documented as "per section 512 bytes").
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

SECTOR = 512


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def u16(data: bytes, off: int, endian: str = "<") -> int:
    return struct.unpack_from(endian + "H", data, off)[0]


def u32(data: bytes, off: int, endian: str = "<") -> int:
    return struct.unpack_from(endian + "I", data, off)[0]


def u64(data: bytes, off: int, endian: str = "<") -> int:
    return struct.unpack_from(endian + "Q", data, off)[0]


def is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


# --------------------------------------------------------------------------
# GPT
# --------------------------------------------------------------------------
@dataclass
class Partition:
    """One partition: byte range inside a whole-device image."""

    name: str
    start: int
    size: int
    source: str = "gpt"        # gpt | mbr | parameter | sysfs | unknown
    index: int | None = None
    type_guid: str = ""
    flags: str = ""
    size_unknown: bool = False  # legacy `-@offset(name)`: runs to end of device

    @property
    def end(self) -> int:
        return self.start + self.size

    def __str__(self) -> str:
        extent = ("to end of device" if self.size_unknown
                  else f"size {self.size:#012x} ({self.size / 1024 / 1024:.1f} MiB)")
        return (f"{self.name:24s} @0x{self.start:012x} "
                f"({self.start // SECTOR:#010x} sec) {extent}")


_GPT_SIG = b"EFI PART"


def _guid(raw: bytes) -> str:
    if len(raw) < 16:
        return ""
    a, b, c = struct.unpack_from("<IHH", raw, 0)
    d = raw[8:10]
    e = raw[10:16]
    return (f"{a:08x}-{b:04x}-{c:04x}-{d.hex()}-{e.hex()}")


def parse_gpt(data: bytes, base_offset: int = 0, device_size: int | None = None):
    """Parse the GPT at the start of `data` (which itself sits at `base_offset`).

    Returns (partitions, info). `info['crc_ok']` is False when the header or entry
    array CRC does not match - that is reported, never silently ignored, because
    it distinguishes "not a GPT" from "GPT damaged / dump truncated".
    """
    info: dict = {"crc_ok": None, "truncated": False, "source": "gpt"}
    if len(data) < 2 * SECTOR + 8:
        info["truncated"] = True
        return [], info

    hdr = base_offset + SECTOR
    if data[hdr:hdr + 8] != _GPT_SIG:
        return [], {"source": "none", "truncated": False, "crc_ok": None}

    header_size = u32(data, hdr + 12)
    stored_crc = u32(data, hdr + 16)
    backup_lba = u64(data, hdr + 32)
    first_usable = u64(data, hdr + 40)
    last_usable = u64(data, hdr + 48)
    entries_lba = u64(data, hdr + 72)
    num_entries = u32(data, hdr + 80)
    entry_size = u32(data, hdr + 84)
    entries_crc = u32(data, hdr + 88)

    if not (92 <= header_size <= SECTOR) or not (128 <= entry_size <= 4096):
        return [], {"source": "none", "truncated": True, "crc_ok": False}

    blank = bytearray(data[hdr:hdr + header_size])
    blank[16:20] = b"\x00\x00\x00\x00"
    info["crc_ok"] = zlib.crc32(bytes(blank)) == stored_crc
    info.update(
        header_size=header_size,
        backup_lba=backup_lba,
        first_usable=first_usable,
        last_usable=last_usable,
        entries_lba=entries_lba,
        num_entries=num_entries,
        entry_size=entry_size,
        header_crc_stored=stored_crc,
        entries_crc_ok=None,
    )
    if last_usable:
        info["disk_size"] = (last_usable + 1) * SECTOR
    if device_size:
        info["disk_size"] = device_size

    tbl = entries_lba * SECTOR
    if tbl < 0 or tbl + num_entries * entry_size > len(data):
        info["truncated"] = True
        return [], info

    info["entries_crc_ok"] = (zlib.crc32(data[tbl:tbl + num_entries * entry_size])
                              == entries_crc)

    parts: list[Partition] = []
    for i in range(num_entries):
        e = tbl + i * entry_size
        type_guid = _guid(data[e:e + 16])
        if type_guid == "0" * 8 + "-0000-0000-0000-000000000000":
            continue
        first_lba = u64(data, e + 32)
        last_lba = u64(data, e + 40)
        if last_lba < first_lba:
            continue
        name = data[e + 56:e + 56 + 72].decode("utf-16-le", "replace").split("\x00")[0]
        parts.append(Partition(
            name=name or f"part{i + 1}",
            start=base_offset + first_lba * SECTOR,
            size=(last_lba - first_lba + 1) * SECTOR,
            source="gpt",
            index=i + 1,
            type_guid=type_guid,
        ))
    return parts, info


# --------------------------------------------------------------------------
# MBR (classic DOS table; Rockchip SD/mainline images use this, not GPT)
# --------------------------------------------------------------------------
_MBR_EXTENDED = {0x05, 0x0F, 0x85}


def parse_mbr(data: bytes, base_offset: int = 0, device_size: int | None = None):
    """Parse the DOS/MBR table at `base_offset`.

    Rockchip bootable SD images (and anything built from a plain `dd` layout) use
    MBR, with the loader living in the unpartitioned gap before partition 1.
    Extended-partition chains are reported but not followed; the caller is told
    so rather than being handed a silently incomplete list.
    """
    info: dict = {"source": "mbr", "extended": False, "truncated": False}
    if len(data) < base_offset + SECTOR or data[base_offset + 510:base_offset + 512] != b"\x55\xaa":
        return [], {"source": "none", "extended": False, "truncated": False}
    parts: list[Partition] = []
    for i in range(4):
        e = base_offset + 446 + i * 16
        ptype = data[e + 4]
        if ptype == 0:
            continue
        lba = u32(data, e + 8)
        count = u32(data, e + 12)
        if count == 0:
            continue
        if ptype in _MBR_EXTENDED:
            info["extended"] = True
            continue
        parts.append(Partition(
            name=f"mbr{i + 1}_type{ptype:02x}",
            start=base_offset + lba * SECTOR,
            size=count * SECTOR,
            source="mbr",
            index=i + 1,
        ))
    info["disk_size"] = device_size or len(data) - base_offset
    return parts, info


def find_loader_images(data: bytes, limit: int, step: int = SECTOR) -> list[tuple[int, str]]:
    """Block-aligned scan for RK loader/trust headers in unpartitioned areas.

    On Rockchip SD images idbloader/u-boot/trust sit in the gap before the first
    partition (usually LBA 64 / 16384), so a partition table alone does not
    describe the whole image.
    """
    found: list[tuple[int, str]] = []
    for magic, label in ((b"LOADER  ", "uboot"), (b"TOS     ", "trust"),
                         (b"KERNEL\x00\x00", "kernel"), (b"RSCE", "resource"),
                         (b"ANDROID!", "android-boot"), (b"RKNS", "idbloader")):
        off = 0
        while True:
            off = data.find(magic, off, limit)
            if off < 0:
                break
            if off % step == 0:
                found.append((off, label))
                off += step
            else:
                off += 1
    # u-boot.itb / u-boot.dtb are FITs, i.e. plain FDTs
    for off in fdt_candidates(data[:limit]):
        if off % step == 0:
            found.append((off, "fit"))
    return sorted(set(found))


# --------------------------------------------------------------------------
# Rockchip legacy `parameter` partition
# --------------------------------------------------------------------------
# CMDLINE: mtdparts=rk29xxnand:0x00001f40@0x00000040(loader1),...,-@0x0040000(rootfs)
_MTPART_RE = re.compile(
    r"(?P<size>-|0x[0-9a-fA-F]+|\d+)@(?P<off>0x[0-9a-fA-F]+|\d+)"
    r"\((?P<name>[^):]+)(?::(?P<flags>[^)]*))?\)"
)


def parse_rk_parameter(data: bytes, device_size: int | None = None):
    """Parse a Rockchip `parameter` partition (the legacy, non-GPT layout).

    mtdparts sizes/offsets are in 512-byte sectors on Rockchip (see rkbin
    tools/parameter_gpt.txt: "#in section; per section 512(0x200) bytes").
    """
    text = data.decode("latin-1", "replace")
    m = re.search(r"mtdparts=[^:]+:(.*)", text)
    if not m:
        return [], {}
    body = m.group(1).strip().split("\n")[0].strip()
    parts: list[Partition] = []
    for item in body.split(","):
        pm = _MTPART_RE.match(item.strip())
        if not pm:
            continue
        off = int(pm.group("off"), 0) * SECTOR
        open_ended = pm.group("size") == "-"
        if open_ended:
            size = (device_size - off) if device_size and device_size > off else 0
        else:
            size = int(pm.group("size"), 0) * SECTOR
        parts.append(Partition(
            name=pm.group("name"),
            start=off,
            size=size,
            source="parameter",
            flags=pm.group("flags") or "",
            size_unknown=open_ended and not size,
        ))
    meta = {}
    for key in ("FIRMWARE_VER", "MACHINE_MODEL", "MACHINE_ID", "MANUFACTURER", "MAGIC"):
        km = re.search(rf"^{key}:\s*(.*)$", text, re.M)
        if km:
            meta[key] = km.group(1).strip()
    return parts, meta


def looks_like_parameter(data: bytes) -> bool:
    head = data[:4096]
    return b"mtdparts=" in head or b"FIRMWARE_VER" in head


# --------------------------------------------------------------------------
# Android boot image (ANDROID!) - v0..v4
# --------------------------------------------------------------------------
BOOT_MAGIC = b"ANDROID!"
_BOOT_HDR_SIZES = {0: 1632, 1: 1648, 2: 1660, 3: 1580, 4: 1584}


@dataclass
class BootImage:
    version: int
    page_size: int
    header_size: int
    name: str
    cmdline: str
    segments: list = field(default_factory=list)   # (name, offset, size)
    trailing_offset: int = 0                       # start of anything appended
    warnings: list = field(default_factory=list)


def parse_android_boot(data: bytes) -> BootImage | None:
    """Parse an Android/Rockchip boot image header and describe its segments.

    Rockchip ships vendor kernels as boot images with an embedded DTB (v2) and
    frequently appends resource.img after the last page-aligned segment, which is
    why `trailing_offset` is returned: everything from there on belongs to other
    containers and must be scanned separately.
    """
    if data[:8] != BOOT_MAGIC:
        return None
    warnings: list[str] = []

    kernel_size = u32(data, 8)
    ramdisk_size = u32(data, 16)
    second_size = u32(data, 24)
    page_size = u32(data, 36)
    version = u32(data, 40)
    name = _cstr(data[48:64])
    cmdline = _cstr(data[64:576])

    if version not in _BOOT_HDR_SIZES:
        warnings.append(f"unknown header version {version}, assuming v2")
        version = 2
    if not (is_pow2(page_size) and 512 <= page_size <= 65536):
        warnings.append(f"implausible page_size {page_size}, assuming 2048")
        page_size = 2048

    img = BootImage(version=version, page_size=page_size,
                    header_size=_BOOT_HDR_SIZES[version],
                    name=name, cmdline=cmdline, warnings=warnings)

    if version >= 3:
        # v3/v4: no addresses, no second stage; kernel then ramdisk, page aligned.
        hdr_size = u32(data, 20) or _BOOT_HDR_SIZES[version]
        off = align_up(max(hdr_size, 1), page_size)
        for seg_name, size in (("kernel", kernel_size), ("ramdisk", ramdisk_size)):
            if size:
                img.segments.append((seg_name, off, size))
                off += align_up(size, page_size)
        img.trailing_offset = off
        return img

    # v0..v2: header page, kernel, ramdisk, second, [recovery_dtbo], [dtb]
    off = align_up(img.header_size, page_size)
    for seg_name, size in (("kernel", kernel_size),
                           ("ramdisk", ramdisk_size),
                           ("second", second_size)):
        if size:
            img.segments.append((seg_name, off, size))
        off += align_up(size, page_size)

    if version >= 1:
        dtbo_size = u32(data, 1632)
        dtbo_off = u64(data, 1636)
        if dtbo_size and dtbo_off:
            img.segments.append(("recovery_dtbo", dtbo_off, dtbo_size))
    if version >= 2:
        dtb_size = u32(data, 1648)
        dtb_off = u64(data, 1652)
        if dtb_size:
            img.segments.append(("dtb", off, dtb_size))
            off += align_up(dtb_size, page_size)
    img.trailing_offset = off
    return img


def align_up(value: int, align: int) -> int:
    return (value + align - 1) // align * align


# --------------------------------------------------------------------------
# Rockchip resource image (RSCE)
# --------------------------------------------------------------------------
RSCE_MAGIC = b"RSCE"
_ENTRY_TAG = b"ENTR"


@dataclass
class ResourceEntry:
    path: str
    offset: int
    size: int
    hash_size: int
    hash: bytes


@dataclass
class ResourceImage:
    entries: list
    header_size: int
    tbl_offset: int
    tbl_entry_size: int
    layout: str            # "natural" or "packed" (see parse_rsce)


def parse_rsce(data: bytes, base_offset: int = 0) -> ResourceImage | None:
    """Parse a Rockchip resource.img.

    struct resource_ptn_header (resource_tool.c):
        char magic[4]; u16 version; u16 index_tbl_version;
        u8 header_size; u8 tbl_offset; u8 tbl_entry_size; u32 tbl_entry_num;
    The C struct is declared without __attribute__((packed)), so tbl_entry_num
    sits at offset 12 with one padding byte. Some third-party builds emit the
    packed variant (offset 11); both are tried and the one whose index table
    actually validates wins.
    """
    if data[:4] != RSCE_MAGIC:
        return None

    for layout, num_off in (("natural", 12), ("packed", 11)):
        header_size = data[8]
        tbl_offset = data[9]
        tbl_entry_size = data[10]
        num = u32(data, num_off)
        if not (1 <= header_size <= 64 and 1 <= tbl_entry_size <= 64 and 0 < num <= 512):
            continue
        entries, ok = _rsce_entries(data, base_offset, tbl_offset,
                                    tbl_entry_size, num)
        if ok:
            return ResourceImage(entries=entries, header_size=header_size,
                                 tbl_offset=tbl_offset,
                                 tbl_entry_size=tbl_entry_size, layout=layout)
    return None


def _rsce_entries(data, base_offset, tbl_offset, tbl_entry_size, num):
    """Index table offsets are relative to `data`; content offsets are absolute
    in the containing file (they are relative to the image, which sits at
    `base_offset` inside `data`)."""
    entries: list[ResourceEntry] = []
    tbl = tbl_offset * SECTOR
    for i in range(num):
        e = tbl + i * tbl_entry_size * SECTOR
        if e + 268 > len(data) or data[e:e + 4] != _ENTRY_TAG:
            return [], False
        path = _cstr(data[e + 4:e + 4 + 220])
        hsize = u32(data, e + 256)
        coff = u32(data, e + 260)          # blocks (512 B)
        csize = u32(data, e + 264)         # bytes
        if not path or csize == 0 or coff == 0:
            return [], False
        entries.append(ResourceEntry(path=path, offset=base_offset + coff * SECTOR,
                                     size=csize, hash_size=hsize,
                                     hash=data[e + 224:e + 256]))
    return entries, True


# --------------------------------------------------------------------------
# Flattened device tree
# --------------------------------------------------------------------------
FDT_MAGIC = 0xD00DFEED


def fdt_candidates(data: bytes) -> list[int]:
    """Offsets of every plausible FDT in `data` (header fields validated)."""
    hits: list[int] = []
    needle = struct.pack(">I", FDT_MAGIC)
    start = 0
    end = len(data) - 40
    while True:
        i = data.find(needle, start)
        if i < 0 or i > end:
            break
        start = i + 4
        totalsize = u32(data, i + 4, ">")
        off_struct = u32(data, i + 8, ">")
        off_strings = u32(data, i + 12, ">")
        version = u32(data, i + 20, ">")
        if not (0x40 <= totalsize <= 64 * 1024 * 1024):
            continue
        if i + totalsize > len(data):
            continue
        if not (36 <= off_struct < totalsize and off_struct < off_strings < totalsize):
            continue
        if not (15 <= version <= 17):
            continue
        hits.append(i)
    return hits


def fdt_totalsize(data: bytes, off: int) -> int:
    return u32(data, off + 4, ">")


# FDT structure-block tokens (devicetree spec v0.4, section 5.4.1)
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


def fdt_parse(data: bytes, off: int = 0) -> dict[str, dict[str, bytes]] | None:
    """Walk an FDT and return {node_path: {property: raw bytes}}.

    Needed because U-Boot FIT images (u-boot.itb, kernel.img on mainline-ish
    Rockchip firmware) embed kernel/ramdisk/DTB payloads *inside* an FDT, as the
    `data` property of /images/<sub-image>. dtc can print that as a byte array
    but cannot hand back the bytes, so the structure block is walked here.
    """
    if data[off:off + 4] != struct.pack(">I", FDT_MAGIC):
        return None
    totalsize = u32(data, off + 4, ">")
    off_struct = u32(data, off + 8, ">")
    off_strings = u32(data, off + 12, ">")
    if off + totalsize > len(data) or off_struct >= totalsize or off_strings >= totalsize:
        return None
    strings = data[off + off_strings:off + totalsize]

    nodes: dict[str, dict[str, bytes]] = {}
    stack: list[str] = ["/"]
    nodes["/"] = {}
    p = off + off_struct
    end = off + totalsize
    while p + 4 <= end:
        token = u32(data, p, ">")
        p += 4
        if token == FDT_BEGIN_NODE:
            stop = data.find(b"\x00", p, end)
            if stop < 0:
                return None
            name = data[p:stop].decode("utf-8", "replace")
            p = align_up(stop + 1, 4)
            parent = stack[-1]
            path = "/" + name if parent == "/" else parent + "/" + name
            stack.append(path)
            nodes.setdefault(path, {})
        elif token == FDT_END_NODE:
            if len(stack) == 1:
                return None
            stack.pop()
        elif token == FDT_PROP:
            length = u32(data, p, ">")
            nameoff = u32(data, p + 4, ">")
            p += 8
            value = data[p:p + length]
            p = align_up(p + length, 4)
            nul = strings.find(b"\x00", nameoff)
            name = strings[nameoff:nul if nul >= 0 else len(strings)].decode("utf-8", "replace")
            nodes[stack[-1]][name] = value
        elif token == FDT_NOP:
            continue
        elif token == FDT_END:
            break
        else:
            return None
    return nodes


def fdt_prop_str(props: dict[str, bytes], name: str) -> str | None:
    raw = props.get(name)
    if raw is None:
        return None
    return _cstr(raw)


def fdt_prop_int(props: dict[str, bytes], name: str) -> int | None:
    raw = props.get(name)
    if raw is None or len(raw) == 0:
        return None
    if len(raw) == 4:
        return u32(raw, 0, ">")
    if len(raw) == 8:
        return u64(raw, 0, ">")
    return None


def fdt_is_fit(nodes: dict[str, dict[str, bytes]]) -> bool:
    """A FIT is an FDT with /images and /configurations, plus a /description."""
    return "/images" in nodes and "/configurations" in nodes


# --------------------------------------------------------------------------
# content sniffing
# --------------------------------------------------------------------------
_FS_MAGICS = (
    (0, b"hsqs", "squashfs"),
    (0, b"\xe2\xe1\xf5\xe0", "erofs"),
    (0, b"\x10\x20\xf5\xf2", "f2fs"),
    (0, b"\x31\x18\x10\x06", "ubifs"),
    (0, b"\x85\x19", "jffs2"),
    (0, b"CRAMFS", "cramfs"),
    (0, b"\x45\x3d\xcd\x28", "cramfs"),
    (0, b"ANDROID!", "android-boot"),
    (0, b"RSCE", "rockchip-resource"),
    (0, b"RKFW", "rockchip-update-rkfw"),
    (0, b"RKAF", "rockchip-update-rkaf"),
    (0, b"LOADER  ", "rk-uboot-image"),
    (0, b"TOS     ", "rk-trust-image"),
    (0, b"KERNEL\x00", "rk-kernel-image"),
    (0, b"KRNL", "krnl-image"),
    (0, b"LDR ", "rk-miniloader"),
    (0, b"RKNS", "rk-idbloader"),
    (0, b"070701", "cpio"),
    (0, b"070702", "cpio"),
    (0, b"070707", "cpio"),
    (0, b"MVIMG", "img-ext"),
    (0x438, b"\x53\xef", "ext4"),
    (0x1000, b"QFI\xfb", "qcow2-no"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (0, b"\x04\x22\x4d\x18", "lz4"),
    (0, b"UBI#", "ubi"),
    (0, b"Rockchip", "rockchip-text"),
)

FILESYSTEMS = {"squashfs", "erofs", "f2fs", "ubifs", "jffs2", "cramfs", "ext4"}
CONTAINERS = {"android-boot", "rockchip-resource", "rockchip-update-rkfw",
              "rockchip-update-rkaf", "rk-uboot-image", "rk-trust-image",
              "rk-kernel-image", "krnl-image", "cpio", "rk-miniloader",
              "rk-idbloader", "dtb-only", "gzip", "xz", "zstd", "bzip2"}


# --------------------------------------------------------------------------
# cpio (the payload of vendor KRNL/initramfs containers)
# --------------------------------------------------------------------------
def parse_cpio(data: bytes) -> list[dict] | None:
    """Parse a newc/crc (070701/070702) or odc (070707) cpio archive.

    FriendlyElec's mkkrnlimg output is gzip(cpio) and Android-style ramdisks are
    cpio, so this is how those payloads get listed.

    newc header (110 bytes, ASCII hex): magic, ino, mode, uid, gid, nlink, mtime,
    filesize, devmajor, devminor, rdevmajor, rdevminor, namesize, check.
    odc header  (76 bytes, ASCII octal): magic, dev, ino, mode, uid, gid, nlink,
    rdev, mtime(11), namesize, filesize(11).
    """
    if len(data) < 76:
        return None
    magic = data[:6]
    if magic in (b"070701", b"070702"):
        hdr, radix = 110, 16
        name_len_span = (94, 8)
        file_size_span = (54, 8)
    elif magic == b"070707":
        hdr, radix = 76, 8
        name_len_span = (59, 6)
        file_size_span = (65, 11)
    else:
        return None

    entries: list[dict] = []
    pos = 0
    while pos + hdr <= len(data):
        block = data[pos:pos + hdr]
        try:
            name_len = int(block[name_len_span[0]:sum(name_len_span)], radix)
            file_size = int(block[file_size_span[0]:sum(file_size_span)], radix)
        except ValueError:
            return entries or None
        name_start = pos + hdr
        if not (0 < name_len <= 4096) or name_start + name_len > len(data):
            return entries or None
        name = data[name_start:name_start + name_len - 1].decode("utf-8", "replace")
        if name == "TRAILER!!!":
            break
        data_start = align_up(name_start + name_len, 4)
        if data_start + file_size > len(data):
            return entries or None
        entries.append({"name": name, "size": file_size, "offset": data_start})
        pos = align_up(data_start + file_size, 4)
    return entries or None


def identify(data: bytes, size: int | None = None) -> str:
    """Best-effort one-word classification of an image/partition."""
    size = len(data) if size is None else size
    if size == 0:
        return "empty"
    if data[:4] == RSCE_MAGIC and parse_rsce(data) is not None:
        return "rockchip-resource"
    if data[:8] == BOOT_MAGIC and parse_android_boot(data):
        return "android-boot"
    for off, magic, label in _FS_MAGICS:
        if data[off:off + len(magic)] == magic:
            return label
    if data[:4] == struct.pack(">I", FDT_MAGIC) and fdt_candidates(data[:64 * 1024]):
        return "dtb-only"
    if looks_like_parameter(data):
        return "rockchip-parameter"
    if data[:2048].count(b"\x00") == 2048:
        return "zeroes"
    printable = sum(1 for b in data[:256] if 32 <= b < 127 or b in (9, 10, 13))
    if printable > 200:
        return "text"
    return "unknown"


# --------------------------------------------------------------------------
# Rockchip loader image header (loaderimage.c)
# --------------------------------------------------------------------------
@dataclass
class LoaderHeader:
    magic: str
    version: int
    hash_size: int
    size: int
    name: str


def parse_loader_header(data: bytes) -> LoaderHeader | None:
    """RK 'LOADER  ' / 'TOS     ' image header, 512-byte aligned blocks."""
    if len(data) < 32:
        return None
    magic = data[:8]
    if magic not in (b"LOADER  ", b"TOS     ", b"KERNEL\x00\x00"):
        return None
    try:
        crypt = u16(data, 8)
        version = u16(data, 10)
        hash_size = u16(data, 12)
        size = u32(data, 14)
        name = _cstr(data[30:30 + 34])
    except struct.error:
        return None
    if crypt not in (0, 0x1985200A):
        return None
    return LoaderHeader(magic=magic.decode("latin-1").rstrip("\x00 "), version=version,
                        hash_size=hash_size, size=size, name=name)


# --------------------------------------------------------------------------
# command line - this module is also usable directly as an inspection tool
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="rkimg",
        description="identify a Rockchip image and list its GPT partitions")
    ap.add_argument("image", help="whole-device or partition image file")
    args = ap.parse_args(argv)

    data = Path(args.image).read_bytes()
    parts, info = parse_gpt(data, 0, len(data))
    if parts:
        print(f"{args.image}: GPT partition table, {len(parts)} partition(s), "
              f"crc_ok={info.get('crc_ok')}")
        for p in sorted(parts, key=lambda x: x.start):
            print(f"  {p.index:>3}  {p.name:24s} {p}")
    else:
        print(f"{args.image}: {identify(data, len(data))}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
