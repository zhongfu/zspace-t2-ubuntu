#!/usr/bin/env python3
"""Build the FAT boot tree that mainline U-Boot's bootstd reads off a boot
partition.

This builds the FAT boot tree that mainline U-Boot's bootstd reads off a boot
partition, in either of two forms:

  * `--out <file>` - a FAT *image* (`boot.vfat`), the artifact the eMMC-side
    writer dd's onto the new p3 (`boot`, FAT); `scripts/t2-flash.sh` ships it
    to an already-partitioned eMMC.  Unchanged, byte-for-byte.
  * `--out-dir <dir>` - the same tree as *files*, which is what the
    card-driven installer carries inside its config FAT (`scripts/t2-image.py
    --boot-dir`).  The installer copies them into a freshly `mkfs.vfat`-ed
    eMMC p3 instead of dd-ing an image; `--out-dir` also emits the two
    extlinux descriptors described below.

`--out-dir` writes a second descriptor, `/extlinux/t2-emmc.conf`: the
`extlinux.conf` file is the *card* descriptor (its `default` is the
`t2-installer` flash entry when `--flash-append` is given, so the card's own
boot selects flash mode), while `t2-emmc.conf` keeps `t2-emmc` as `default` and
is the file the installer copies over the eMMC's `/extlinux/extlinux.conf` -
so the installed disk boots the primary entry and the installer never edits a
conf.  Both are built from the same `conf_text()` inputs the FAT image uses.

Inputs are the real ones - the kernel `Image` and a board DTB - plus the kernel
cmdline; the conf is generated here, not copied, so the entry can never drift
from the files that sit next to it.

Contents (exactly these, nothing else):
    /Image
    /<dtb basename>
    /extlinux/extlinux.conf
    /extlinux/t2-emmc.conf     (only with --out-dir)
    /Image.old                 (only with --fallback-image)
    /uboot.env                 (only with --fallback-image)

`--fallback-image <path>` adds the A/B fallback of notes/kernel-updates.md
section 4: it copies the previous kernel to `/Image.old` and adds the fallback
extlinux label (`t2-emmc-old`) that boots it with the same cmdline.  The
primary entry (`t2-emmc`, `/Image`) stays the `default`, so U-Boot's normal
bootstd scan is unchanged; only when the boot counter exceeds `bootlimit`
does U-Boot run `altbootcmd`, which loads `/Image.old` directly (env defaults
compiled into the board defconfig
`workbench/uboot-rk3568/uboot/configs/t2-rk3568_defconfig`).  The option is
opt-in: without it the image is byte-for-byte the three-file tree it always
was.

The default `--append` is the defect-D1-safe spelling: `root=LABEL=zspace-rootfs`,
never the short `PARTUUID=` form.  A short PARTUUID was measured to resolve to
the *wrong* device (`notes/distro-image.md` section 2.3/D1, and section 4.5 for
why the label is the only spelling that names one partition when the card and
the eMMC carry the same PARTUUID).

The image is written with mtools (mformat/mmd/mcopy) and then verified by
reading it back with the same tools, so a file that does not survive VFAT - or a
partition too small to hold it - fails here rather than on the board.

Usage:
    scripts/t2-boot-fat.py --image Image --dtb rk3568-t2.dtb --out boot.vfat
    scripts/t2-boot-fat.py --image Image --dtb rk3568-t2.dtb \
        --label T2-BOOT --size 256M --out boot.vfat
    # installer card: make the initramfs' flash mode the default entry
    scripts/t2-boot-fat.py --image Image --dtb rk3568-t2.dtb --out boot.vfat \
        --flash-append t2.mode=flash --default-entry t2-installer
    # the same tree as files, for the card's config FAT (both descriptors)
    scripts/t2-boot-fat.py --image Image --dtb rk3568-t2.dtb \
        --out-dir /mnt/card --flash-append t2.mode=flash \
        --fallback-image Image.old

`--flash-append` adds a second entry (`t2-installer`) whose cmdline puts the
embedded initramfs into its installer flash mode - the card-driven install path
now runs there instead of in the installed rootfs.  It is written first and
`--default-entry` names it, so U-Boot's bootstd reaches the installer through
exactly the same scan a normal card uses for the rootfs.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"t2-boot-fat: error: {msg}", file=sys.stderr, flush=True)
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


DEFAULT_LABEL = "T2-BOOT"
# The bootstd entry's label (the *FAT volume* label is `--label`, and the two
# are independent: this one is the menu entry, that one is what the partition
# is called).
CONF_LABEL = "t2-emmc"
# The second entry: boot the initramfs' installer flash mode instead of a
# rootfs.  It exists so an installer card needs no manual menu interaction -
# the card's extlinux.conf can make it the `default`.
FLASH_LABEL = "t2-installer"
# The A/B fallback entry and file: the previous kernel, kept next to the new
# one so U-Boot's altbootcmd can boot it when the boot counter is exceeded
# (notes/kernel-updates.md section 4).  See --fallback-image.
FALLBACK_LABEL = "t2-emmc-old"
FALLBACK_KERNEL_NAME = "Image.old"
CONF_DIR = "extlinux"
CONF_NAME = "extlinux.conf"
# The second descriptor `--out-dir` writes: the eMMC's `/extlinux/` file.  The
# installer copies this over `extlinux.conf` on the freshly formatted eMMC p3,
# so the installed disk boots `t2-emmc` while the card's own descriptor boots
# the installer (`t2-installer`) - the installer never edits a conf.
EMMC_CONF_NAME = "t2-emmc.conf"
KERNEL_NAME = "Image"
# The persistent U-Boot environment, written with the A/B kernel so a freshly
# installed kernel is *armed* before its first boot.  U-Boot's `env_t` is a
# 4-byte CRC32 of the data (native word order: little-endian on this ARM
# board) followed by the NUL-separated `key=value` list, padded to
# CONFIG_ENV_SIZE; `env_fat_save()` writes exactly `sizeof(env_t)` bytes
# (env/fat.c:76-85, `env_export()` in env/common.c).  `ENV_SIZE` here is
# CONFIG_ENV_SIZE from workbench/uboot-rk3568/uboot/configs/t2-rk3568_defconfig.
ENV_NAME = "uboot.env"
ENV_SIZE = 0x1f000
ENV_DATA_SIZE = ENV_SIZE - 4
# The compiled default environment of that same U-Boot build (`make
# u-boot-initial-env`): the env blob must carry it whole, or U-Boot would lose
# `bootcmd`/`altbootcmd`/`bootlimit`/the memory addresses and stop booting.
ENV_DEFAULTS = REPO / "build" / "out" / "u-boot-initial-env"
# See the module docstring: a short PARTUUID boots the wrong partition.
# `rootflags=` carries the root mount options: the distro's /etc/fstab
# deliberately does not name the root device at all (the installer repartitions
# the eMMC, so a baked PARTUUID would be stale), and the initramfs passes this
# through to the mount it performs (workbench/initramfs-bringup/init).
DEFAULT_APPEND = ("root=LABEL=zspace-rootfs rootfstype=ext4 rw rootwait "
                  "rootflags=errors=remount-ro panic=15")
# The flash entry deliberately carries no root= at all: /init sees
# `t2.mode=flash` in /proc/cmdline before it resolves any root device, so a
# root= here would only risk booting a rootfs by accident.
DEFAULT_FLASH_APPEND = "t2.mode=flash"
# Auto size is the contents plus room for the FAT tables and the directory
# entries; "a few MiB" is generous for a 60 MB kernel, and anyone who wants the
# artifact to fill a fixed-size partition passes --size.
SLACK = 8 << 20
# FAT stops at 4 GiB - 1 (that is also why the card's payload partition is ext4
# and not FAT; notes/distro-image.md section 4.7).
FAT32_LIMIT = (4 << 30) - 1
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,11}\Z")


def run(*argv: object) -> subprocess.CompletedProcess:
    """Run an mtools command, dying with its output if it fails."""
    args = [str(a) for a in argv]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"{args[0]} failed ({r.returncode}):\n{r.stdout}{r.stderr}")
    return r


def probe(*argv: object) -> subprocess.CompletedProcess:
    """Like run(), but a non-zero exit is the caller's business."""
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True)


def check_inputs(image: Path, dtb: Path, fallback: Path | None = None) -> None:
    pairs = [("--image", image), ("--dtb", dtb)]
    if fallback is not None:
        pairs.append(("--fallback-image", fallback))
    for what, p in pairs:
        if not p.exists():
            die(f"{what} {p}: no such file")
        if not p.is_file():
            die(f"{what} {p}: not a regular file")
    if image.stat().st_size == 0:
        die(f"--image {image}: empty file")
    if fallback is not None and fallback.stat().st_size == 0:
        die(f"--fallback-image {fallback}: empty file")


def validate(label: str, dtb_name: str, append: str,
             flash_append: str | None = None) -> None:
    if not LABEL_RE.match(label):
        die(f"--label {label!r}: a FAT volume label is 1-11 characters from "
            f"A-Z a-z 0-9 . _ - (the default is {DEFAULT_LABEL})")
    # The two names sit at the FAT root next to each other, so a collision
    # would silently lose one of them.
    if "/" in dtb_name or dtb_name in ("", ".", ".."):
        die(f"--dtb {dtb_name!r}: not a usable file name")
    if dtb_name != dtb_name.strip() or any(c.isspace() for c in dtb_name):
        # extlinux's "fdt <path>" is whitespace-separated; a DTB whose name has
        # a space in it could not be referenced from the conf at all.
        die(f"--dtb {dtb_name!r}: a DTB name with whitespace cannot be "
            f"referenced from extlinux.conf")
    if len(dtb_name.encode()) > 255:
        die(f"--dtb {dtb_name!r}: name longer than VFAT allows (255 bytes)")
    if dtb_name in (CONF_DIR, KERNEL_NAME, FALLBACK_KERNEL_NAME):
        die(f"--dtb basename {dtb_name!r} collides with /{CONF_DIR}, "
            f"/{KERNEL_NAME} or /{FALLBACK_KERNEL_NAME} on the boot partition")
    if any(c in "\r\n" for c in append):
        die("--append: a newline would split the extlinux.conf entry")
    if flash_append is not None and any(c in "\r\n" for c in flash_append):
        die("--flash-append: a newline would split the extlinux.conf entry")


def conf_text(dtb_name: str, append: str, flash_append: str | None = None,
              default: str | None = None, fallback: bool = False) -> bytes:
    """The generated bootstd entry/entries.  Kept in one place so the file
    written and the bytes verified are the same construction.

    With `flash_append` set, a second entry (`t2-installer`) boots the
    initramfs' installer flash mode instead of a rootfs.  It is written
    *first* and `default` names it, so an installer card reaches the installer
    through the same bootstd path a normal card uses for the rootfs - no menu
    interaction, no second medium.

    With `fallback` set, a final entry (`t2-emmc-old`) boots `/Image.old`, the
    previous kernel kept for U-Boot's bootcount fallback.  `altbootcmd` loads
    the file directly; the entry documents the slot and lets a human at the
    bootstd menu pick it.  It is never the `default`.
    """
    parts = []
    if default is not None:
        parts.append(f"default {default}\n")
    if flash_append is not None:
        parts.append(f"label {FLASH_LABEL}\n"
                     f"\tkernel /{KERNEL_NAME}\n"
                     f"\tfdt /{dtb_name}\n"
                     f"\tappend {flash_append}\n")
    parts.append(f"label {CONF_LABEL}\n"
                 f"\tkernel /{KERNEL_NAME}\n"
                 f"\tfdt /{dtb_name}\n"
                 f"\tappend {append}\n")
    if fallback:
        parts.append(f"label {FALLBACK_LABEL}\n"
                     f"\tkernel /{FALLBACK_KERNEL_NAME}\n"
                     f"\tfdt /{dtb_name}\n"
                     f"\tappend {append}\n")
    return "".join(parts).encode()


def conf_pair(dtb_name: str, append: str, flash_append: str | None,
              fallback: bool) -> tuple[bytes, bytes]:
    """The two extlinux descriptors of an `--out-dir` tree.

    The card descriptor (`extlinux/extlinux.conf`) makes the `t2-installer`
    flash entry the `default` when one exists (so the card boots into the
    installer), and the eMMC descriptor (`extlinux/t2-emmc.conf`) keeps
    `t2-emmc` as the `default` (both entries are present, so the eMMC can still
    reach flash mode from the bootstd menu).  Both come from `conf_text()`, the
    same construction the FAT image uses.
    """
    card_default = FLASH_LABEL if flash_append is not None else CONF_LABEL
    return (conf_text(dtb_name, append, flash_append, card_default, fallback),
            conf_text(dtb_name, append, flash_append, CONF_LABEL, fallback))


def env_blob(defaults: Path) -> bytes:
    """The `/uboot.env` blob to ship with an A/B kernel.

    `defaults` is the board build's compiled default environment
    (`u-boot-initial-env`); the blob keeps it whole and changes two values:

      * `upgrade_available=1` - arms the boot counter for the kernel just
        written.  `bootcount_env.c` only persists a count while
        `upgrade_available != 0` (drivers/bootcount/bootcount_env.c:12-16), so
        without this the fallback never triggers;
      * `bootcount=0` - start the count at zero.

    Writing this file is what makes a freshly flashed eMMC protected on its
    very first boot, before any userspace has run.
    """
    entries = {}
    for line in defaults.read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        entries[key] = val
    entries["upgrade_available"] = "1"
    entries["bootcount"] = "0"
    data = b"".join(f"{k}={v}\0".encode() for k, v in entries.items())
    if len(data) > ENV_DATA_SIZE:
        die(f"environment does not fit /{ENV_NAME} "
            f"({len(data)} > {ENV_DATA_SIZE} bytes)")
    data = data.ljust(ENV_DATA_SIZE, b"\0")
    return struct.pack("<I", zlib.crc32(data) & 0xffffffff) + data


def resolve_size(want: str, contents: int) -> int:
    if contents > FAT32_LIMIT:
        die(f"contents {human(contents)} exceed the FAT32 limit "
            f"({human(FAT32_LIMIT)}): use ext4 for a payload this big")
    if want == "auto":
        # round up to whole MiB so the image is sector-aligned and its size is
        # the sort of number a partition table wants (t2-image.py does the same)
        size = (contents + SLACK + (1 << 20) - 1) // (1 << 20) * (1 << 20)
        if size > FAT32_LIMIT:
            die(f"contents {human(contents)} do not fit a FAT32 image even "
                f"with the {human(SLACK)} of slack (limit {human(FAT32_LIMIT)})")
        return size
    size = parse_size(want)
    if size < contents:
        die(f"--size {human(size)} cannot hold the contents ({human(contents)})")
    if size > FAT32_LIMIT:
        die(f"--size {human(size)} is over the FAT32 limit "
            f"({human(FAT32_LIMIT)})")
    return size


def build(out: Path, label: str, size: int, image: Path, dtb: Path,
          conf: bytes, fallback: Path | None = None,
          env: bytes | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    subprocess.run(["truncate", "-s", str(size), str(out)], check=True)
    # -F is what makes it FAT32; mtools would otherwise pick FAT12/16 for a
    # small volume, and bootstd is after a FAT *filesystem*, so make the type
    # explicit rather than depend on a size heuristic.
    run("mformat", "-i", out, "-F", "-v", label, "::")
    run("mmd", "-i", out, f"::/{CONF_DIR}")
    run("mcopy", "-i", out, image, f"::/{KERNEL_NAME}")
    if fallback is not None:
        run("mcopy", "-i", out, fallback, f"::/{FALLBACK_KERNEL_NAME}")
    run("mcopy", "-i", out, dtb, f"::/{dtb.name}")
    with tempfile.TemporaryDirectory(prefix="t2-boot-fat-") as tmp:
        td = Path(tmp)
        if env is not None:
            ef = td / ENV_NAME
            ef.write_bytes(env)
            run("mcopy", "-i", out, ef, f"::/{ENV_NAME}")
        cf = td / CONF_NAME
        cf.write_bytes(conf)
        run("mcopy", "-i", out, cf, f"::/{CONF_DIR}/{CONF_NAME}")


def fat_bytes(out: Path, member: str, tmp: Path) -> bytes:
    """Read one member back as the bytes mtools hands to a reader."""
    run("mcopy", "-i", out, f"::/{member}", tmp)
    return tmp.read_bytes()


def verify(out: Path, label: str, image: Path, dtb: Path, conf: bytes,
           fallback: Path | None = None, env: bytes | None = None) -> list:
    """Read the finished image back with mtools; return (name, ok, detail)."""
    checks = []
    info = probe("minfo", "-i", out, "::")
    vol_label = ""
    fs_type = ""
    for line in info.stdout.splitlines():
        if line.startswith("disk label="):
            vol_label = line.split("=", 1)[1].strip().strip('"').strip()
        elif line.startswith("disk type="):
            fs_type = line.split("=", 1)[1].strip().strip('"').strip()
    checks.append((f"FAT32 volume labelled {label}",
                   fs_type.startswith("FAT32") and vol_label == label,
                   f"minfo says type={fs_type!r} label={vol_label!r}"))
    # The root must hold exactly the three things bootstd needs - an extra
    # directory entry is a hint something was copied in that should not be
    # there (and a missing one means bootstd would find nothing to boot).
    listing = probe("mdir", "-i", out, "-b", "::")
    entries = sorted(line.strip() for line in listing.stdout.splitlines()
                     if line.strip().startswith("::/"))
    want = sorted([f"::/{KERNEL_NAME}", f"::/{dtb.name}", f"::/{CONF_DIR}/"]
                  + ([f"::/{FALLBACK_KERNEL_NAME}"] if fallback is not None
                     else [])
                  + ([f"::/{ENV_NAME}"] if env is not None else []))
    names = ("root is exactly " + (f"/{ENV_NAME}, " if env is not None else "")
             + f"/{KERNEL_NAME}"
             + (f", /{FALLBACK_KERNEL_NAME}" if fallback is not None else "")
             + ", /<dtb>, /extlinux")
    checks.append((names, entries == want,
                   f"mdir says {', '.join(entries) or 'nothing'}"))
    with tempfile.TemporaryDirectory(prefix="t2-boot-fat-") as tmp:
        tmpd = Path(tmp)
        got = fat_bytes(out, KERNEL_NAME, tmpd / "Image")
        want_sha = sha256_file(image)
        got_sha = hashlib.sha256(got).hexdigest()
        checks.append((f"/{KERNEL_NAME} sha256 == {image.name}",
                       got_sha == want_sha,
                       f"{got_sha[:16]} vs {want_sha[:16]} "
                       f"({len(got):,} of {image.stat().st_size:,} B)"))
        if fallback is not None:
            got = fat_bytes(out, FALLBACK_KERNEL_NAME, tmpd / "Image.old")
            want_sha = sha256_file(fallback)
            got_sha = hashlib.sha256(got).hexdigest()
            checks.append((f"/{FALLBACK_KERNEL_NAME} sha256 == {fallback.name}",
                           got_sha == want_sha,
                           f"{got_sha[:16]} vs {want_sha[:16]} "
                           f"({len(got):,} of {fallback.stat().st_size:,} B)"))
        got = fat_bytes(out, dtb.name, tmpd / "board.dtb")
        checks.append((f"/{dtb.name} == {dtb.name}", got == dtb.read_bytes(),
                       f"{len(got):,} of {dtb.stat().st_size:,} bytes read back"))
        got = fat_bytes(out, f"{CONF_DIR}/{CONF_NAME}", tmpd / CONF_NAME)
        checks.append((f"/{CONF_DIR}/{CONF_NAME} == generated text",
                       got == conf,
                       f"{len(got)} of {len(conf)} bytes read back"))
        if env is not None:
            got = fat_bytes(out, ENV_NAME, tmpd / ENV_NAME)
            checks.append((f"/{ENV_NAME} == armed env blob ({len(env)} B)",
                           got == env,
                           f"{len(got)} of {len(env)} bytes read back, "
                           f"upgrade_available=1"))
    # The FDT path is the one thing a reader of the FAT image does not itself
    # check, so assert the generated text really names the DTB that was written.
    fdt_line = next((l for l in conf.decode().splitlines()
                     if l.startswith("\tfdt ")), "")
    checks.append((f"entry points at /{dtb.name}",
                   fdt_line == f"\tfdt /{dtb.name}",
                   f"conf says {fdt_line.strip()!r}"))
    if fallback is not None:
        text = conf.decode()
        checks.append((f"conf has the {FALLBACK_LABEL} entry booting "
                       f"/{FALLBACK_KERNEL_NAME}",
                       f"label {FALLBACK_LABEL}\n" in text and
                       f"\tkernel /{FALLBACK_KERNEL_NAME}\n" in text,
                       f"conf lists {text.count('label ')} labels"))
        cur = next((l.split(None, 1)[1] for l in text.splitlines()
                    if l.startswith("default ")), "no default")
        checks.append((f"{FALLBACK_LABEL} is not the default entry",
                       cur != FALLBACK_LABEL,
                       f"conf default={cur!r}"))
    return checks


def write_out_dir(out_dir: Path, image: Path, dtb: Path, card_conf: bytes,
                  emmc_conf: bytes, fallback: Path | None = None,
                  env: bytes | None = None) -> None:
    """Write the boot tree as *files* (the card's config FAT root).

    Same inputs as `build()` - one code path for the same validation and
    `conf_text()` construction - but no FAT image: the installer later copies
    these files into a freshly `mkfs.vfat`-ed eMMC p3.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CONF_DIR).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, out_dir / KERNEL_NAME)
    shutil.copyfile(dtb, out_dir / dtb.name)
    if fallback is not None:
        shutil.copyfile(fallback, out_dir / FALLBACK_KERNEL_NAME)
    if env is not None:
        (out_dir / ENV_NAME).write_bytes(env)
    (out_dir / CONF_DIR / CONF_NAME).write_bytes(card_conf)
    (out_dir / CONF_DIR / EMMC_CONF_NAME).write_bytes(emmc_conf)


def conf_default(text: str) -> str:
    """The value of the extlinux `default` directive, or '' when absent."""
    for line in text.splitlines():
        if line.startswith("default "):
            return line.split(None, 1)[1]
    return ""


def verify_tree(out_dir: Path, image: Path, dtb: Path, card_conf: bytes,
                emmc_conf: bytes, flash: bool, fallback: Path | None = None,
                env: bytes | None = None) -> list:
    """Read the written tree back from the directory; return (name, ok, detail)."""
    checks = []
    expect = {KERNEL_NAME: image, dtb.name: dtb}
    if fallback is not None:
        expect[FALLBACK_KERNEL_NAME] = fallback
    missing = [rel for rel in expect if not (out_dir / rel).is_file()]
    missing += [f"{CONF_DIR}/{n}" for n in (CONF_NAME, EMMC_CONF_NAME)
                if not (out_dir / CONF_DIR / n).is_file()]
    if env is not None and not (out_dir / ENV_NAME).is_file():
        missing.append(ENV_NAME)
    checks.append(("tree holds every enumerated file", not missing,
                   f"missing: {', '.join(missing)}" if missing
                   else ", ".join("/" + rel for rel in
                                  ([ENV_NAME] if env is not None else [])
                                  + list(expect) + [CONF_DIR + "/"])))
    if missing:
        return checks
    for rel, src in expect.items():
        p = out_dir / rel
        got = sha256_file(p)
        want = sha256_file(src)
        checks.append((f"/{rel} sha256 == {src.name}", got == want,
                       f"{got[:16]} vs {want[:16]} "
                       f"({p.stat().st_size:,} of {src.stat().st_size:,} B)"))
    if env is not None and (out_dir / ENV_NAME).is_file():
        got = (out_dir / ENV_NAME).read_bytes()
        checks.append((f"/{ENV_NAME} == armed env blob ({len(env)} B)",
                       got == env,
                       f"{len(got)} of {len(env)} bytes read back, "
                       f"upgrade_available=1"))
    got_card = (out_dir / CONF_DIR / CONF_NAME).read_bytes()
    got_emmc = (out_dir / CONF_DIR / EMMC_CONF_NAME).read_bytes()
    checks.append((f"/{CONF_DIR}/{CONF_NAME} == card descriptor",
                   got_card == card_conf,
                   f"{len(got_card)} of {len(card_conf)} bytes read back"))
    checks.append((f"/{CONF_DIR}/{EMMC_CONF_NAME} == eMMC descriptor",
                   got_emmc == emmc_conf,
                   f"{len(got_emmc)} of {len(emmc_conf)} bytes read back"))
    card_default = FLASH_LABEL if flash else CONF_LABEL
    checks.append((f"card descriptor selects {card_default}",
                   conf_default(got_card.decode()) == card_default,
                   f"default={conf_default(got_card.decode())!r}"))
    checks.append((f"eMMC descriptor selects {CONF_LABEL}",
                   conf_default(got_emmc.decode()) == CONF_LABEL,
                   f"default={conf_default(got_emmc.decode())!r}"))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(
        description="build the FAT boot tree (Image + dtb + extlinux.conf) "
                    "that mainline U-Boot's bootstd reads")
    out_grp = ap.add_mutually_exclusive_group(required=True)
    out_grp.add_argument("--out", type=Path, default=None,
                    help="FAT image to write (e.g. boot.vfat)")
    out_grp.add_argument("--out-dir", type=Path, default=None,
                    help="directory to write the boot tree as *files*: "
                         f"/{KERNEL_NAME}, /<dtb>, "
                         f"/{CONF_DIR}/{CONF_NAME} (card descriptor, default "
                         f"{FLASH_LABEL} with --flash-append) and "
                         f"/{CONF_DIR}/{EMMC_CONF_NAME} (eMMC descriptor, "
                         f"default {CONF_LABEL}), plus "
                         f"/{FALLBACK_KERNEL_NAME} and /{ENV_NAME} with "
                         "--fallback-image.  No FAT image is written")
    ap.add_argument("--image", type=Path, required=True,
                    help="kernel Image, copied to /Image")
    ap.add_argument("--dtb", type=Path, required=True,
                    help="board DTB, copied to /<basename>")
    ap.add_argument("--fallback-image", type=Path, default=None,
                    help="previous kernel: also copy it to /"
                         f"{FALLBACK_KERNEL_NAME} and add the "
                         f"{FALLBACK_LABEL} extlinux entry, so U-Boot's "
                         "bootcount fallback (altbootcmd) has something to "
                         "boot.  Not written when absent")
    ap.add_argument("--env-defaults", type=Path, default=ENV_DEFAULTS,
                    help="the board build's compiled default environment "
                         f"(default {ENV_DEFAULTS.relative_to(REPO)}): with "
                         "--fallback-image it is written to /"
                         f"{ENV_NAME} with upgrade_available=1 and "
                         "bootcount=0, arming the boot counter for the kernel "
                         "just written")
    ap.add_argument("--label", default=DEFAULT_LABEL,
                    help=f"FAT volume label (default {DEFAULT_LABEL})")
    ap.add_argument("--append", default=DEFAULT_APPEND,
                    help="kernel cmdline for the extlinux entry (default: the "
                         "defect-D1-safe root=LABEL=zspace-rootfs form)")
    ap.add_argument("--flash-append", default=None,
                    help="also write a second extlinux entry (label "
                         f"{FLASH_LABEL}) with this cmdline - the initramfs' "
                         f"installer flash mode; e.g. the default "
                         f"{DEFAULT_FLASH_APPEND!r}.  Not written when absent")
    ap.add_argument("--default-entry",
                    choices=(CONF_LABEL, FLASH_LABEL), default=None,
                    help="emit the extlinux `default` directive.  Default: "
                         "none, so a reader without `default` support takes "
                         f"the first entry ({FLASH_LABEL} is only valid with "
                         "--flash-append)")
    ap.add_argument("--size", default="auto",
                    help="image size (auto = contents + slack, or e.g. 256M)")
    args = ap.parse_args()

    if args.out_dir is not None and args.default_entry is not None:
        die("--default-entry applies to --out only: --out-dir always writes "
            f"/{CONF_DIR}/{CONF_NAME} with the card default and "
            f"/{CONF_DIR}/{EMMC_CONF_NAME} with {CONF_LABEL}")

    if args.default_entry == FLASH_LABEL and args.flash_append is None:
        die(f"--default-entry {FLASH_LABEL} needs --flash-append")
    check_inputs(args.image, args.dtb, args.fallback_image)
    dtb_name = args.dtb.name
    validate(args.label, dtb_name, args.append, args.flash_append)

    fallback = args.fallback_image
    env = None
    if fallback is not None:
        # Arm the counter *in the tree*: a freshly flashed eMMC must be
        # protected on its first boot, before any userspace has run.  The
        # committed blob is U-Boot's own compiled default env plus
        # upgrade_available=1 / bootcount=0, so U-Boot keeps bootcmd,
        # altbootcmd, bootlimit and the memory addresses.
        if not args.env_defaults.exists():
            die(f"--env-defaults {args.env_defaults}: no such file (a "
                f"U-Boot build's `make u-boot-initial-env` output)")
        env = env_blob(args.env_defaults)

    if args.out_dir is not None:
        card_conf, emmc_conf = conf_pair(dtb_name, args.append,
                                         args.flash_append, fallback is not None)
        flash = args.flash_append is not None
        log(f"-- plan: boot tree files in {args.out_dir} --")
        log(f"  /{CONF_DIR}/{CONF_NAME}: card descriptor, "
            f"default={FLASH_LABEL if flash else CONF_LABEL}")
        log(f"  /{CONF_DIR}/{EMMC_CONF_NAME}: eMMC descriptor, "
            f"default={CONF_LABEL}")
        files = [(f"/{KERNEL_NAME}", args.image.stat().st_size)]
        if fallback is not None:
            files.append((f"/{FALLBACK_KERNEL_NAME}",
                          fallback.stat().st_size))
            files.append((f"/{ENV_NAME}", len(env)))
        files += [(f"/{dtb_name}", args.dtb.stat().st_size),
                  (f"/{CONF_DIR}/{CONF_NAME}", len(card_conf)),
                  (f"/{CONF_DIR}/{EMMC_CONF_NAME}", len(emmc_conf))]
        for name, n in files:
            log(f"  {name:28s} {human(n):>18}")
        write_out_dir(args.out_dir, args.image, args.dtb, card_conf,
                      emmc_conf, fallback, env)

        log("-- verify (read back from the directory) --")
        checks = verify_tree(args.out_dir, args.image, args.dtb, card_conf,
                             emmc_conf, flash, fallback, env)
        bad = [c for c in checks if not c[1]]
        for name, ok, detail in checks:
            log(f"  [{'ok' if ok else 'FAIL'}] {name}  ({detail})")
        if bad:
            die(f"{len(bad)} check(s) failed")
        # both generated descriptors, for the log: they are the pieces
        # nothing else shows
        for line in card_conf.decode().splitlines():
            log(f"  {CONF_NAME} | {line}")
        for line in emmc_conf.decode().splitlines():
            log(f"  {EMMC_CONF_NAME} | {line}")
        log(f"wrote the boot tree into {args.out_dir} - "
            f"all {len(checks)} checks passed")
        return 0

    conf = conf_text(dtb_name, args.append, args.flash_append,
                     args.default_entry, fallback is not None)
    contents = args.image.stat().st_size + args.dtb.stat().st_size + len(conf)
    if fallback is not None:
        contents += fallback.stat().st_size
    if env is not None:
        contents += len(env)
    size = resolve_size(args.size, contents)

    log(f"-- plan: {args.out} FAT32 labelled {args.label}, {human(size)} --")
    entries = [CONF_LABEL]
    if args.flash_append is not None:
        entries.insert(0, FLASH_LABEL)
    if fallback is not None:
        entries.append(FALLBACK_LABEL)
    if args.default_entry is not None:
        log(f"  /{CONF_DIR}/{CONF_NAME}: {len(entries)} entries "
            f"({', '.join(entries)}), default={args.default_entry}")
    else:
        log(f"  /{CONF_DIR}/{CONF_NAME}: {len(entries)} entries "
            f"({', '.join(entries)})")
    files = [(f"/{KERNEL_NAME}", args.image.stat().st_size)]
    if fallback is not None:
        files.append((f"/{FALLBACK_KERNEL_NAME}", fallback.stat().st_size))
        files.append((f"/{ENV_NAME}", len(env)))
    files += [(f"/{dtb_name}", args.dtb.stat().st_size),
              (f"/{CONF_DIR}/{CONF_NAME}", len(conf))]
    for name, n in files:
        log(f"  {name:28s} {human(n):>18}")
    if size < contents:
        die(f"the computed size {human(size)} is below the contents")

    build(args.out, args.label, size, args.image, args.dtb, conf,
          fallback, env)

    log("-- verify (read back with mtools) --")
    checks = verify(args.out, args.label, args.image, args.dtb, conf,
                    fallback, env)
    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        log(f"  [{'ok' if ok else 'FAIL'}] {name}  ({detail})")
    if bad:
        die(f"{len(bad)} check(s) failed")
    # the generated conf, for the log: it is the one piece nothing else shows
    for line in conf.decode().splitlines():
        log(f"  conf | {line}")
    log(f"wrote {args.out} ({human(args.out.stat().st_size)}) - "
        f"all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
