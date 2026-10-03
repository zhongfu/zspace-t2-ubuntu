#!/usr/bin/env python3
"""t2-build.py - build a complete, flashable ZSpace T2 (RK3568) bundle.

This is the single entrypoint for the whole build flow, which the per-stage
scripts and `docs/building.md` describe step by step:

    kernel : make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -jN Image dtbs
             -> <out>/Image + <out>/rk3568-t2.dtb
    fit    : images/rk-fit.py --kernel Image --dtb rk3568-t2.dtb --out ...
             -> <out>/t2-mainline-boot.img   (vendor-compatible unsigned FIT:
                fdt@0x800, kernel at 0x800+dtb size, sha256 per image,
                rollback-index 0, configuration `conf`) - written to p3
                (`boot`, 64 MiB, GPT LBA 0x8000)
    rootfs : ubuntu-base-<ver>-base-arm64.tar.gz (fetched and sha256-verified)
             -> <out>/rootfs-stage/   extracted tree (intermediate, kept)
             -> <out>/rootfs.ext4     ext4 image sized for p11
                (`source_rootfs`, GPT LBA 0x36a8000, 1,925,152,768 B)
    manifest: <out>/manifest.json + <out>/SHA256SUMS  (sha256 of every artifact,
             the inputs that produced them, and a chunked flash plan)

Nothing in this tool touches the board.  Flashing is a separate opt-in step:
`--flash` invokes the existing tools/t2-flash.py for the FIT; the rootfs
image is written with rkdeveloptool `wl` (the chunk plan, with the exact LBAs,
is in manifest.json).

Build isolation (why the default is not the primary tree)
---------------------------------------------------------
Two `make` runs in one kernel tree corrupt each other's archives (observed
2026-09-30 00:29: `vmlinux.a: member net/ipv4/udp.o in archive is not an
object`, two concurrent builds).  `make O=<dir>` cannot rescue that here
because the kernel refuses an out-of-tree build while the source tree holds
in-tree build state (`Makefile:outputmakefile`).  So the kernel step builds in
a private checkout by default (`--sync-tree`, a shared-object clone of the
source tree + its uncommitted board DTS/Makefile/.config); `-O DIR` and
`--no-sync-tree` are available for a pristine or idle tree.  A `flock` on the
build root serialises t2-build runs.

Invariants carried over from the bring-up notes
-----------------------------------------------
* A DTS change moves the kernel inside the FIT (kernel offset = 0x800 + dtb
  size), so the FIT is always rebuilt as a whole and the resulting offsets and
  per-image sha256 are read back out of the finished image and recorded.
* The FIT must fit the 64 MiB boot partition, and t2-flash.py writes it in
  <=8 MiB rockusb `wl` chunks, so the exact byte size is the gate this tool
  checks (it fails loudly, with the headroom, if it does not fit).
* `Image` embeds the bring-up initramfs (CONFIG_INITRAMFS_SOURCE), so the
  initramfs is part of the kernel hash and is recorded in the manifest.
* The kernel build needs PATH / LD_LIBRARY_PATH / BISON_PKGDATADIR /
  CROSS_COMPILE; those are set from the extracted cross toolchain under tools/,
  never from the host.

Usage
-----
This file is the shared pipeline module.  It lives at ``<repo>/lib/t2-build.py``
and is loaded by ``rootfs/t2-distro.py`` (which reuses ``Runner``,
``build_env``, ``sha256_file``, ``parse_size``, ``artifact``, ``flash_plan`` and
``config_problems``).  It can also be run as a CLI:

    lib/t2-build.py --all            # everything (default)
    lib/t2-build.py --kernel-only    # Image + DTB + FIT + manifest
    lib/t2-build.py --rootfs-only    # tarball + stage + rootfs.ext4 + manifest
    lib/t2-build.py --all --dry-run  # print the plan, change nothing

    lib/t2-build.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Fixed facts about this project / board (provenance in comments)
# --------------------------------------------------------------------------
# This module lives at <repo>/lib/t2-build.py.  Resolve every path from its own
# location so it works from any clone, whatever the current directory is.
SCRIPTS = Path(__file__).resolve().parent   # <repo>/lib (also holds rkimg.py)
ROOT = SCRIPTS.parent                       # the repository root

KERNEL_TREE = ROOT / "build" / "kernel"      # kernel/fetch.sh clones here
OUT_DIR = ROOT / "build" / "out"             # finished artefacts
# Kernel build output.  None = in-tree, matching kernel/build.sh; a cross
# toolchain is taken from CROSS_ROOT when present, otherwise from the host PATH.
BUILD_DIR = None
# Where --sync-tree puts a private checkout when the primary tree is dirty with
# in-tree build state (shared-object clone; no object duplication).
SYNC_TREE = ROOT / "build" / "tree"
PRIMARY_TREE = KERNEL_TREE          # the source of truth, never built by us
ROOTFS_CACHE = ROOT / "build" / "rootfs"

CROSS_PREFIX = "aarch64-linux-gnu-"
CROSS_ROOT = ROOT / "tools" / "cross" / "root"          # optional vendored gcc
CROSS_BIN = CROSS_ROOT / "usr" / "bin"
CROSS_LIB = CROSS_ROOT / "usr" / "lib" / "x86_64-linux-gnu"
DTC = ROOT / "tools" / "dtc-root" / "usr" / "bin" / "dtc"
RK_FIT = ROOT / "images" / "rk-fit.py"
T2_FLASH = ROOT / "tools" / "t2-flash.py"
RKDEVELOPTOOL = ROOT / "tools" / "rkdeveloptool" / "build" / "rkdeveloptool"

# GPT, parsed from a full vendor flash dump (supports the vendor layout):
BOOT_LBA = 0x8000               # p3 "boot"
BOOT_SIZE = 64 * 1024 * 1024    # p3 = 67,108,864 B
ROOTFS_LBA = 0x36A8000          # p11 "source_rootfs"
ROOTFS_SIZE = 1925152768        # p11 = 3,760,064 sectors * 512
FLASH_CHUNK = 8 * 1024 * 1024   # rockusb `wl` chunk used by t2-flash.py
FIT_STRUCT_ALIGN = 0x800        # rk-fit.py: FIT structure is padded to 0x800

# tools/t2-flash.py: T2_TTY/T2_LOG defaults; keep in sync if the adapter moves.
T2_TTY = os.environ.get("T2_TTY", "/dev/ttyUSB0")
T2_LOG = os.environ.get("T2_LOG", "/tmp/zspace/serial-live.log")

# Ubuntu base rootfs (26.04.1 LTS, Resolute Raccoon).  The sha256 is the arm64
# entry of the upstream SHA256SUMS shipped next to the tarball (cdimage).
UBUNTU_RELEASE = "26.04.1"
UBUNTU_VERSION = "26.04.1"
UBUNTU_BASE_URL = (f"https://cdimage.ubuntu.com/ubuntu-base/releases/"
                   f"{UBUNTU_RELEASE}/release")
UBUNTU_TARBALL = f"ubuntu-base-{UBUNTU_VERSION}-base-arm64.tar.gz"
UBUNTU_SHA256 = \
    "5a1906794ced63a71a8119c3f211ef5f0bbe0a243001b4bbd41fdf80c5b219fd"
ROOTFS_LABEL = "zspace-rootfs"

# `make ARCH=arm64 defconfig` + these symbols reproduces the board config that
# is documented in docs/building.md.  The tree's
# own .config is the ground truth; --bootstrap-config is only for a fresh
# checkout.  Symbols are validated against the live .config by `--check-config`.
BOOTSTRAP_ENABLE = """
ARCH_ROCKCHIP PCIE_ROCKCHIP_DW_HOST PHY_ROCKCHIP_NANENG_COMBO_PHY
NVME_CORE BLK_DEV_NVME TYPEC TYPEC_TCPM TYPEC_TCPCI TYPEC_FUSB302
USB_USBNET USB_NET_AX88179_178A USB_RTL8152 USB_NET_CDCETHER USB_NET_CDC_NCM
USB_NET_SMSC95XX USB_DWC3 USB_XHCI_HCD USB_XHCI_PLATFORM USB_CONFIGFS
USB_F_ECM USB_F_NCM USB_LIBCOMPOSITE USB_U_ETHER R8169
ROCKCHIP_THERMAL ROCKCHIP_SARADC RTC_DRV_PCF8563 RTC_DRV_HYM8563
IIO_ST_MAGN_3AXIS IIO_ST_MAGN_I2C_3AXIS SENSORS_GPIO_FAN SENSORS_PWM_FAN
SPI_ROCKCHIP_SFC DRM_ROCKCHIP DRM_DW_HDMI CFG80211 MAC80211 BRCMFMAC
""".split()
# Media/drm modules the tree currently builds as =m.  HW video decode would
# want CONFIG_VIDEO_ROCKCHIP_VDEC=m too (notes mention it) but the tree's
# .config has it off today, so it is deliberately not part of the curated set.
BOOTSTRAP_MODULE = "VIDEO_ROCKCHIP_RGA DRM_DW_HDMI_I2S_AUDIO".split()

STEPS = ("kernel", "fit", "rootfs")

# ---- config trimming (2026-10-01 audit) ----------------------------------
# The tree's .config is `make ARCH=arm64 defconfig` - the *multiplatform*
# defconfig, which enables every vendor platform (its lines 36-87) - plus the
# curated symbols above.  The audit (agent://KernelTrimAudit, KernelTrimAudit2)
# measured ~14 MB of the 60 MB Image as silicon this board does not have:
# other-vendor clock controllers (~5.8 MB) and pinctrl (~4.0 MB) dominate,
# then ACPI, KVM/Xen/virtio, the SCSI HBA families, ATA, TV/DVB media,
# other-SoC USB host controllers, and other vendors' NIC/WLAN/GPU/audio
# *modules* (~60 MB of /lib/modules).  Disabling the other arm64 *platforms*
# is the lever: olddefconfig then drops whole vendor subtrees by dependency.
# Applied by `--trim-config`; `--check-config` insists that TRIM_REQUIRE
# survives, because this Image boots with *no* /lib/modules - a load-bearing
# driver silently lost to a wrong config is the failure mode worth guarding
# (2026-10-01: a stale config produced exactly that - no HEVC decoder, no
# PCIe WiFi - and it looked completely normal).
TRIM_DISABLE = """
# other arm64 platforms (arch/arm64/Kconfig.platforms); RK3568 = ARCH_ROCKCHIP
ARCH_ACTIONS ARCH_AIROHA ARCH_ALPINE ARCH_APPLE ARCH_ARTPEC ARCH_ASPEED
ARCH_AXIADO ARCH_BCM2835 ARCH_BCMBCA ARCH_BCM_IPROC ARCH_BERLIN ARCH_BITMAIN
ARCH_BLAIZE ARCH_BRCMSTB ARCH_BST ARCH_CIX ARCH_EXYNOS ARCH_HISI
ARCH_INTEL_SOCFPGA ARCH_K3 ARCH_KEEMBAY ARCH_LAN969X ARCH_LAYERSCAPE ARCH_LG1K
ARCH_MA35 ARCH_MEDIATEK ARCH_MESON ARCH_MMP ARCH_MVEBU ARCH_MXC ARCH_NPCM
ARCH_PENSANDO ARCH_QCOM ARCH_REALTEK ARCH_RENESAS ARCH_S32 ARCH_SEATTLE
ARCH_SOPHGO ARCH_SPARX5 ARCH_SPRD ARCH_STM32 ARCH_SUNXI ARCH_SYNQUACER
ARCH_TEGRA ARCH_TESLA_FSD ARCH_THUNDER ARCH_THUNDER2 ARCH_UNIPHIER
ARCH_VEXPRESS ARCH_VISCONTI ARCH_XGENE ARCH_ZYNQMP
# firmware tables instead of device tree? no: boot log says "ACPI: Interpreter
# disabled." on every boot, U-Boot passes no tables, the DTS is the only source
ACPI
# bare metal, no hypervisor, no guests, no virtio device anywhere
KVM VIRTUALIZATION XEN VIRTIO VFIO_PCI
# storage transports this board cannot have: no HBA, no external PCIe slot,
# no onboard SATA controller (sata1/sata2 are status="disabled" in the base
# DTS and both combo PHYs are spoken for).  A USB-SATA enclosure does NOT use
# these - it terminates in usb-storage/UAS (SCSI core + sd_mod), which stays.
MEGARAID_SAS SCSI_HISI_SAS SCSI_HISI_SAS_PCI SCSI_SAS_LIBSAS
SCSI_SAS_ATTRS SCSI_SAS_ATA SCSI_SAS_HOST_SMP SCSI_LOWLEVEL SCSI_MPT3SAS
ATA SATA_AHCI SATA_AHCI_PLATFORM SATA_SIL24
# media beyond the Rockchip decoder: no tuner, no SDR, no camera fitted
MEDIA_ANALOG_TV_SUPPORT MEDIA_DIGITAL_TV_SUPPORT MEDIA_SDR_SUPPORT DVB_CORE
# nothing here mounts these, and there is no swap for hibernation
SQUASHFS UBIFS_FS 9P_FS NET_9P NET_9P_VIRTIO HIBERNATION IP_VS CAN NFC
# other-SoC USB host controllers (this board is dwc3 + ehci/ohci/xhci)
USB_DWC2 USB_CHIPIDEA USB_MUSB_HDRC USB_ISP1760 USB_MTU3
# /proc/kallsyms without the 235k *data* symbols: ~1.9-2.0 MB of Image
# [INFERENCE from nm symbol counts].  KALLSYMS itself stays, so oops code
# symbolication and addr2line/nm on vmlinux are unaffected; only data-address
# naming goes away.
KALLSYMS_ALL
# virtio/remoteproc/rpmsg/vhost: bare metal, no guests, no co-processor.
# VIRTIO is `select`ed by its members (drivers/virtio/Kconfig), so turning the
# core off alone is futile - olddefconfig just turns it back on; the members
# have to go with it.
VIRTIO_PCI VIRTIO_PCI_LEGACY VIRTIO_MMIO VIRTIO_BLK VIRTIO_NET
VIRTIO_CONSOLE VIRTIO_BALLOON VHOST_MENU
REMOTEPROC DRM_VIRTIO_GPU I2C_VIRTIO HW_RANDOM_VIRTIO
# VIRTIO and RPMSG are deliberately NOT listed: lingering =m users in this
# config `select` them, so olddefconfig turns the cores back on - the users are
# the thing to disable (see TRIM_MODULE_DISABLE), and what stays is an empty
# core rather than a mismatched assertion.
"""

# Empty on purpose.  NFS was the one =m candidate (=m keeps the capability and
# moves the code out of the Image, ~849 KB) but it does not work here:
# fs/nfs_common/grace.c and fs/nfs/* are selected both by CONFIG_NFS_FS (then
# =m) and by other built-in selectors through NFS_COMMON's `def_bool y`, so
# modpost rejects the duplicate exports - 'locks_start_grace' exported twice,
# 'previous export was in vmlinux', and the same for the nfs client symbols.
# The Kconfig select graph decides this, not the trim, so NFS stays =y and
# TRIM_REQUIRE now pins it there.
TRIM_MODULE = """
"""

# =n for *modules* whose hardware cannot be attached: shrinks /lib/modules
# (349 MB apparent of the 6 GiB rootfs) without touching the Image.
TRIM_MODULE_DISABLE = """
DRM_NOUVEAU DRM_MSM DRM_VC4 DRM_V3D
MLX5_CORE MLX4_CORE MLX4_EN BNXT E1000 E1000E IGB IXGBE HNS3
ATH10K ATH11K ATH12K IWLWIFI IWLMVM MWIFIEX RTW88 RTW89 RTL8188EE
SND_SOC_TEGRA210_AHUB RAID_ATTRS VIRTIO_FS RPMSG_CHAR RPMSG_VIRTIO
# S4 audit (agent://TrimNet2, 2026-10-01): networking/protocols sized from the
# staged .ko, cross-checked against the boot log and the board DTS.  Two of its
# proposals are deliberately NOT here because the curated lists protect them:
# R8169 and MAC80211 are =y in BOOTSTRAP_ENABLE/TRIM_REQUIRE, and BT_HCIUART_LL
# is on the keep-list (the BT path uses H4+BCM, but LL is a curated keep).
# Ethernet NICs with no way to attach on this board (no PCIe slot is free - the
# three controllers carry WiFi + 2 NVMe - and no such SoC is present):
AMD_XGBE ATL1C BCMGENET BNX2X ENA_ETHERNET IGBVF MACB MVMDIO QCOM_EMAC
RMNET SKY2 SMC91X SMSC911X SYSTEMPORT THUNDER_NIC_PF XILINX_AXI_EMAC
# HNS itself is a hidden tristate selected by these, so disabling the visible
# children is what actually turns the HiSilicon set off
HNS_DSAF HNS_ENET HNS_MDIO
# DSA switch stack: rk3568-t2.dts has no switch/mdio node, and stmmac uses
# plain MDIO PHYs, not DSA
NET_DSA NET_DSA_TAG_BRCM NET_DSA_TAG_BRCM_PREPEND NET_DSA_TAG_OCELOT
NET_DSA_TAG_OCELOT_8021Q B53 NET_DSA_BCM_SF2
# non-brcmfmac wireless (the fitted radio is BRCMFMAC/BCM43752 on pcie2x1, =y)
MT76_CORE MT76_CONNAC_LIB MT792x_LIB MT7921_COMMON MT7921E
WL18XX RSI_91X WCN36XX RTL_CARDS
# Bluetooth transports other than the fitted H4+BCM on serial0
BT_HCIBTUSB BT_HCIRSI BT_INTEL BT_MRVL BT_MRVL_SDIO BT_MTK BT_NXPUART BT_RTL
BT_HCIUART_QCA
"""

# must stay =y after any trim; any miss is a MISMATCH, not a warning
TRIM_REQUIRE = """
VIDEO_ROCKCHIP_VDEC VIDEO_HANTRO VIDEO_HANTRO_ROCKCHIP
MEDIA_SUPPORT VIDEO_DEV V4L2_MEM2MEM_DEV MEDIA_CONTROLLER
BRCMFMAC BRCMFMAC_PCIE CFG80211 MAC80211
BT BT_HCIUART BT_HCIUART_BCM BT_HCIUART_H4 BT_HCIUART_SERDEV
NVME_CORE BLK_DEV_NVME PCIE_ROCKCHIP_DW_HOST PHY_ROCKCHIP_NANENG_COMBO_PHY
MMC MMC_DW MMC_DW_ROCKCHIP MMC_BLOCK
DRM ROCKCHIP_VOP2 ROCKCHIP_DW_HDMI DRM_DW_HDMI
USB_DWC3 USB_XHCI_HCD USB_XHCI_PLATFORM USB_CONFIGFS USB_F_ECM
USB_LIBCOMPOSITE USB_U_ETHER USB_STORAGE
EXT4_FS VFAT_FS BLK_DEV_SD SCSI
WATCHDOG WATCHDOG_CORE CPU_FREQ_GOV_SCHEDUTIL ROCKCHIP_THERMAL
REGULATOR_RK808 MFD_RK8XX RTC_DRV_HYM8563 INPUT_EVDEV HWMON
PINCTRL_ROCKCHIP COMMON_CLK_ROCKCHIP IIO_ST_MAGN_3AXIS SENSORS_GPIO_FAN
MODULES MODULE_UNLOAD IKCONFIG_PROC KALLSYMS MODULE_COMPRESS
MODULE_COMPRESS_ZSTD NFS_FS NFS_V4 SUNRPC LOCKD
# Hang persistence (2026-10-02): the ramoops backend comes from the board DTS
# (/reserved-memory/ramoops@110000) and MUST be built in - the device is
# created at boot from the DT, so a =m backend would only be loadable after
# rootfs, missing an early-boot hang entirely.  The detectors are what name
# the stuck task; a trim that silently drops them would leave the board with
# nothing but the serial console again.
PSTORE PSTORE_RAM PSTORE_CONSOLE PSTORE_PMSG
LOCKUP_DETECTOR SOFTLOCKUP_DETECTOR DETECT_HUNG_TASK HARDLOCKUP_DETECTOR
"""

# values that one step learns and the manifest wants (e.g. real ext4 usage)
LAST: dict = {}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"t2-build: error: {msg}", file=sys.stderr, flush=True)
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


def build_root() -> Path:
    """Where make writes: the O= directory, or the source tree for in-tree."""
    return BUILD_DIR if BUILD_DIR is not None else KERNEL_TREE


def cfg_path() -> Path:
    """The config `--check-config` validates: the kernel tree's `.config`.

    `kernel/build.sh` copies ``kernel/config/kernel.config`` into
    ``build/kernel/.config`` before building, so that file is both what the
    build uses and what the curated symbol lists are checked against.  An
    out-of-tree ``-O DIR`` build would keep its own copy; validating the source
    tree avoids the class of bug where the checked config and the built config
    diverge (2026-10-01).
    """
    return KERNEL_TREE / ".config"


def make_base() -> list:
    args = ["make", "-C", str(KERNEL_TREE)]
    if BUILD_DIR is not None:
        args.append(f"O={BUILD_DIR}")
    args.append("ARCH=arm64")
    return args


def lock_path() -> Path:
    """Lock file *for the build root*, kept out of the source tree in-tree."""
    if BUILD_DIR is not None:
        return BUILD_DIR / ".t2-build.lock"
    key = hashlib.sha256(str(KERNEL_TREE).encode()).hexdigest()[:12]
    return Path("/tmp/t2-build-locks") / f"{KERNEL_TREE.name}-{key}.lock"


def acquire_lock(args):
    """flock the build root so two t2-build runs cannot race each other.

    Note: this only serialises this tool; an out-of-tree --build-dir is the way
    to avoid racing somebody else's hand-run `make` in the same source tree.
    """
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w+")
    import fcntl
    flags = fcntl.LOCK_EX | (0 if args.wait_lock else fcntl.LOCK_NB)
    try:
        fcntl.flock(fh.fileno(), flags)
    except BlockingIOError:
        die(f"another t2-build holds {lock} "
            "(use --wait-lock to queue behind it)")
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    log(f"   build lock  : {lock}")
    return fh


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


def build_env() -> dict:
    """The environment the manual build used (notes, bsp-port.md:23-25).

    A vendored toolchain under ``tools/cross/root`` is prepended when present;
    otherwise ``CROSS_COMPILE`` points at whichever ``aarch64-linux-gnu-``
    toolchain the host PATH provides.
    """
    env = dict(os.environ)
    path = []
    if CROSS_BIN.is_dir():
        path.append(str(CROSS_BIN))
    env["PATH"] = os.pathsep.join(path + [env.get("PATH", "")])
    env["CROSS_COMPILE"] = CROSS_PREFIX
    if CROSS_LIB.is_dir():
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            [str(CROSS_LIB)]
            + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    bison_share = CROSS_ROOT / "usr" / "share" / "bison"
    if bison_share.is_dir():
        env["BISON_PKGDATADIR"] = str(bison_share)
    return env


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def preflight(args, steps, env) -> dict:
    """Verify every external thing we rely on; die on a missing requirement.

    Step-aware: a --rootfs-only run must not require a kernel tree/toolchain,
    and a --kernel-only run must not require the rootfs host tools.
    """
    want_kernel = bool({"kernel", "fit"} & set(steps))
    want_rootfs = "rootfs" in steps
    found = {}
    log("== preflight ==")
    problems = []

    def check(label, path, required=True):
        ok = path is not None and Path(str(path)).exists()
        log(f"  [{'ok' if ok else '--'}] {label:<18} "
            f"{path if path else 'NOT FOUND'}")
        found[label] = str(path) if ok else None
        if required and not ok:
            problems.append(f"missing {label}: {path}")
        return ok

    def which(tool):
        return shutil.which(tool, path=env["PATH"]) or shutil.which(tool)

    if want_kernel:
        check("kernel tree", KERNEL_TREE,
              required=not getattr(args, "sync_tree", None))
        check("kernel .config", cfg_path(), required=False)
        check("rk-fit.py", RK_FIT)
        check("dtc", DTC)
        check("cross gcc", which(CROSS_PREFIX + "gcc"))
        check("bison", which("bison"))
        check("flex", which("flex"))
    if want_rootfs:
        for tool in ("bash", "tar", "curl", "fakeroot", "mke2fs", "e2fsck",
                     "debugfs"):
            check("host " + tool, which(tool))
    check("git", which("git"), required=False)

    log(f"  env CROSS_COMPILE={env['CROSS_COMPILE']} "
        f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '(unset)')} "
        f"BISON_PKGDATADIR={env.get('BISON_PKGDATADIR', '(unset)')}")

    if want_kernel and not cfg_path().exists():
        if args.bootstrap_config:
            log("  [i] no .config - will run --bootstrap-config "
                "(defconfig + documented enables)")
        elif BUILD_DIR is not None and (KERNEL_TREE / ".config").exists():
            log(f"  [i] {cfg_path()} absent - will seed it from "
                f"{KERNEL_TREE}/.config and run olddefconfig")
        else:
            problems.append(f"no .config at {cfg_path()} "
                            "(use --bootstrap-config, or build one first)")
    if problems:
        for p in problems:
            log(f"  [!] {p}")
        die("preflight failed")
    return found


# --------------------------------------------------------------------------
# step: kernel
# --------------------------------------------------------------------------
def bootstrap_config(args, R, env) -> None:
    """Fresh checkout: defconfig + the documented option set."""
    if not args.bootstrap_config:
        die(f"{cfg_path()} is missing; re-run with --bootstrap-config "
            "or provide a .config")
    log("-- kernel: bootstrapping .config (fresh checkout) --")
    R.run(make_base() + ["defconfig"], env=env)
    cfg = KERNEL_TREE / "scripts" / "config"
    R.run([cfg, "--file", cfg_path(), "--enable", *BOOTSTRAP_ENABLE], env=env)
    R.run([cfg, "--file", cfg_path(), "--module", *BOOTSTRAP_MODULE], env=env)
    R.run(make_base() + ["olddefconfig"], env=env)


def ensure_config(args, R, env) -> None:
    """Guarantee a usable .config in the build root before make runs."""
    if cfg_path().exists():
        return
    if args.bootstrap_config:
        bootstrap_config(args, R, env)
        return
    if BUILD_DIR is not None and (KERNEL_TREE / ".config").exists() and not R.dry:
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KERNEL_TREE / ".config", cfg_path())
        log(f"  seeded {cfg_path()} from {KERNEL_TREE}/.config")
        R.run(make_base() + ["olddefconfig"], env=env)
        return
    if R.dry:
        log(f"  [dry] {cfg_path()} missing; would seed/bootstrap it")
        return
    die(f"no .config at {cfg_path()} and nothing to seed it from "
        "(use --bootstrap-config)")


def sync_tree(args, R, env) -> Path:
    """Create/refresh a private source tree for our build.

    Why not just `make O=dir` in the primary tree?  Because the kernel refuses
    an out-of-tree build while the source tree still holds in-tree build state
    (`Makefile` `outputmakefile`: it bails if `<srctree>/.config`,
    `<srctree>/include/config` or `<srctree>/arch/<arch>/include/generated`
    exist), and somebody else is building in-tree in the primary tree.  So
    isolation = a separate checkout (shared-object clone: seconds, ~1.8 GB, no
    object duplication) plus the primary tree's uncommitted board files.
    """
    src = KERNEL_TREE
    dst = Path(args.sync_tree).resolve()
    if R.dry:
        log(f"  [dry] would clone {src} -> {dst} (+ board DTS/Makefile/.config)")
        return dst
    if not (dst / ".git").exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        R.run(["git", "clone", "--shared", "--quiet", str(src), str(dst)],
              env=env)
    # 1. tracked modifications in the primary working tree (the board Makefile
    #    entry that adds rk3568-t2.dtb to dtb-$(CONFIG_ARCH_ROCKCHIP))
    diff = subprocess.run(["git", "-C", str(src), "diff"],
                          capture_output=True, text=True).stdout
    applied = 0
    if diff.strip():
        tmpdir = Path(tempfile.mkdtemp(prefix="t2-build-"))
        try:
            tmp = tmpdir / "tracked.patch"
            tmp.write_text(diff)
            check = subprocess.run(["git", "-C", str(dst), "apply", "--check",
                                    str(tmp)], capture_output=True, text=True)
            if check.returncode == 0:
                R.run(["git", "-C", str(dst), "apply", str(tmp)], env=env)
                applied = 1
            else:
                # Two very different cases: the clone already carries the diff,
                # or the diff does not apply to it at all.  The second one
                # silently produced an unpatched kernel once (2026-10-01: a
                # stale sync tree without patch 0001 kept being reused while
                # this branch just logged "already applied"), so tell them
                # apart instead of guessing: reverse-apply is the discriminator.
                rev = subprocess.run(["git", "-C", str(dst), "apply", "--check",
                                      "--reverse", str(tmp)],
                                     capture_output=True, text=True)
                if rev.returncode == 0:
                    log("  [i] tracked diff already applied in the build tree")
                else:
                    die("tracked diff from " + str(src) + " does not apply to "
                        "the build tree " + str(dst) + " - it is stale; delete "
                        "it (or pass --sync-tree DIR) and re-run.\n"
                        + (check.stderr or "").strip()[-600:])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    # 2. untracked, non-ignored files (the board DTS lives there)
    others = subprocess.run(["git", "-C", str(src), "ls-files", "--others",
                             "--exclude-standard"], capture_output=True,
                            text=True).stdout.split()
    for rel in others:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)
    # 3. the .config (gitignored: in neither list above)
    if (src / ".config").exists():
        shutil.copyfile(src / ".config", dst / ".config")
    log(f"  build tree : {dst}")
    log(f"    cloned from {src}; {applied} tracked diff, "
        f"{len(others)} untracked file(s) + .config synced")
    return dst


def _symbols(text) -> list:
    """Symbol names out of a 'symbols with # comments' block (str or list)."""
    if isinstance(text, (list, tuple)):
        text = "\n".join(text)
    return [w for line in text.splitlines()
            if not line.lstrip().startswith("#")
            for w in line.split()]


def apply_trim() -> None:
    """--trim-config: turn the audited dead weight off in the tree's .config.

    Idempotent: `scripts/config` edits in place and one `olddefconfig` resolves
    the dependency fallout - disabling another arm64 platform drops its whole
    vendor subtree (clocks, pinctrl, media, soc, ...), which is where the audit
    found ~10 MB of this Image.  Only KERNEL_TREE/.config is written; the build
    copies it into the sync tree, so the trim travels with every later build.
    Symbols absent from the config are skipped, and MODULE_COMPRESS_ZSTD is
    enabled because 1,591 modules are ~349 MB apparent inside the 6 GiB rootfs.
    """
    cfg = cfg_path()
    if not cfg.exists():
        die(f"no {cfg} to trim")
    sc = KERNEL_TREE / "scripts" / "config"
    if not sc.exists():
        die(f"no {sc} to edit {cfg}")
    text = cfg.read_text()
    edits = []

    def run(*argv) -> None:
        r = subprocess.run(["/bin/bash", str(sc), "--file", str(cfg), *argv],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f"scripts/config {' '.join(argv)}: {r.stderr.strip()[-300:]}")

    def present(sym: str) -> bool:
        return f"CONFIG_{sym}=" in text or f"# CONFIG_{sym} is not set" in text

    for sym in _symbols(TRIM_DISABLE) + _symbols(TRIM_MODULE_DISABLE):
        if present(sym):
            run("--disable", sym)
            edits.append(f"-{sym}")
    for sym in _symbols(TRIM_MODULE):
        if present(sym):
            run("--module", sym)
            edits.append(f"={sym}=m")
    # 1,591 modules are ~349 MB apparent inside the 6 GiB rootfs, so ship them
    # compressed.  MODULE_COMPRESS is the bool that enables the choice and the
    # algorithm is a separate symbol - olddefconfig drops the algorithm again
    # if the bool is off, which is exactly what happened on the first run.
    run("--enable", "MODULE_COMPRESS")
    run("--enable", "MODULE_COMPRESS_ZSTD")
    edits += ["+MODULE_COMPRESS", "+MODULE_COMPRESS_ZSTD"]
    for sym in ("MODULE_COMPRESS_NONE", "MODULE_COMPRESS_GZIP",
                "MODULE_COMPRESS_XZ"):
        if present(sym):
            run("--disable", sym)
    log("== trim config ==")
    log(f"  {len(edits)} symbols edited in {cfg}; running olddefconfig")
    r = subprocess.run(["make", "-C", str(KERNEL_TREE), "ARCH=arm64",
                        "olddefconfig"], capture_output=True, text=True)
    if r.returncode != 0:
        die("olddefconfig failed:\n" + (r.stderr or r.stdout).strip()[-800:])
    log(f"  .config now {cfg.stat().st_size} B "
        f"sha256 {sha256_file(cfg)}")


def config_problems() -> list[str]:
    """The curated lists against the real config; empty means all match.

    Split out of check_config so the distro driver's modules stage can apply
    the same guard without inheriting --check-config's sys.exit.
    """
    text = cfg_path().read_text()
    bad = []
    for sym in BOOTSTRAP_ENABLE:
        if f"CONFIG_{sym}=y" not in text:
            bad.append(f"CONFIG_{sym} is not =y")
    for sym in BOOTSTRAP_MODULE:
        if f"CONFIG_{sym}=m" not in text and f"CONFIG_{sym}=y" not in text:
            bad.append(f"CONFIG_{sym} is neither =m nor =y")
    require = _symbols(TRIM_REQUIRE)
    disable = _symbols(TRIM_DISABLE) + _symbols(TRIM_MODULE_DISABLE)
    modules = _symbols(TRIM_MODULE)
    for sym in require:
        if f"CONFIG_{sym}=y" not in text:
            bad.append(f"CONFIG_{sym} (load-bearing) is not =y")
    for sym in modules:
        if f"CONFIG_{sym}=m" not in text and f"CONFIG_{sym}=y" not in text:
            bad.append(f"CONFIG_{sym} is neither =m nor =y")
    for sym in disable:
        if f"CONFIG_{sym}=y" in text or f"CONFIG_{sym}=m" in text:
            bad.append(f"CONFIG_{sym} should be off (--trim-config)")
    return bad


def check_config(args) -> None:
    """--check-config: confirm the curated bootstrap list matches the config."""
    cfg = cfg_path()
    if not cfg.exists():
        die(f"no {cfg} to check; prepare the kernel tree first "
            "(kernel/fetch.sh and kernel/build.sh)")
    bad = config_problems()
    log(f"== config check ({cfg}) ==")
    log(f"  {len(BOOTSTRAP_ENABLE)} enable + {len(BOOTSTRAP_MODULE)} module "
        f"symbols, {len(_symbols(TRIM_REQUIRE))} load-bearing and "
        f"{len(_symbols(TRIM_DISABLE) + _symbols(TRIM_MODULE_DISABLE))} "
        "trimmed back to off: "
        f"{'all match' if not bad else 'MISMATCH'}")
    for b in bad:
        log(f"  [!] {b}")
    sys.exit(0 if not bad else 1)


def step_kernel(args, R, out: Path, env) -> dict:
    log("-- kernel: Image + dtbs --")
    ensure_config(args, R, env)
    log(f"  using {cfg_path()} "
        + (f"(sha256 {sha256_file(cfg_path())})" if cfg_path().exists()
           else "(dry)"))
    log(f"  build root  : {build_root()}"
        + (" [in-tree]" if BUILD_DIR is None else " [O=]"))

    # sync_tree() copies .config into the private tree, and that tree's
    # include/config/auto.conf can be older than the copy - kbuild then falls
    # into conf --oldconfig, which prompts and reads EOF on a non-tty.  This
    # is the non-interactive re-sync; it only fills in defaults, and the
    # curated lists are proved intact by --check-config and by the distro
    # driver's modules stage.
    R.run(make_base() + ["olddefconfig"], env=env)
    R.run(make_base() + [f"-j{args.jobs}", "Image", "dtbs"], env=env)

    image = build_root() / "arch" / "arm64" / "boot" / "Image"
    dtb = build_root() / "arch" / "arm64" / "boot" / "dts" / "rockchip" / \
        "rk3568-t2.dtb"
    if R.dry:
        log(f"  [dry] would copy {image} -> {out / 'Image'}")
        log(f"  [dry] would copy {dtb} -> {out / 'rk3568-t2.dtb'}")
        return {"Image": image, "dtb": dtb}
    for src, dst in ((image, out / "Image"), (dtb, out / "rk3568-t2.dtb")):
        if not src.exists():
            die(f"build did not produce {src}")
        shutil.copyfile(src, dst)
        log(f"  {dst.name:<18} {human(dst.stat().st_size)}  "
            f"sha256 {sha256_file(dst)}")
    return {"Image": out / "Image", "dtb": out / "rk3568-t2.dtb"}


# --------------------------------------------------------------------------
# step: FIT
# --------------------------------------------------------------------------
def fit_report(fit: Path) -> dict:
    """Read the finished FIT back the way the bootloader would (rkimg parser)."""
    sys.path.insert(0, str(SCRIPTS))
    import rkimg  # noqa: E402  (shared helper in this directory)

    data = fit.read_bytes()
    nodes = rkimg.fdt_parse(data, 0)
    if nodes is None:
        die(f"{fit} does not parse as a FIT")
    report = {"structure_align": FIT_STRUCT_ALIGN, "images": {},
              "hash_ok": True}
    for name in ("fdt", "kernel", "resource"):
        props = nodes.get(f"/images/{name}")
        if props is None:
            continue
        pos = rkimg.fdt_prop_int(props, "data-position")
        size = rkimg.fdt_prop_int(props, "data-size")
        blob = data[pos:pos + size]
        recorded = nodes.get(f"/images/{name}/hash", {}).get("value")
        good = recorded == hashlib.sha256(blob).digest()
        report["images"][name] = {"offset": pos, "size": size,
                                  "hash_ok": good}
        report["hash_ok"] = report["hash_ok"] and good
    conf = nodes.get("/configurations/conf", {})
    report["config"] = sorted(conf)
    report["default"] = rkimg.fdt_prop_str(nodes["/configurations"], "default")
    report["signed"] = "/configurations/conf/signature" in nodes
    if not report["hash_ok"]:
        die(f"{fit}: embedded sha256 does not match the payloads")
    return report


def step_fit(args, R, out: Path, built: dict) -> dict:
    log("-- fit: pack vendor-compatible FIT --")
    image = out / "Image"
    dtb = out / "rk3568-t2.dtb"
    fit = out / "t2-mainline-boot.img"
    missing = [p for p in (image, dtb) if not p.exists()]
    if missing and not R.dry:
        die(f"missing {', '.join(str(m) for m in missing)}; run the kernel step")

    R.run([sys.executable, RK_FIT, "--kernel", image, "--dtb", dtb,
           "--out", fit])
    if R.dry:
        log(f"  [dry] would verify {fit.name} offsets/hashes and the "
            f"{human(BOOT_SIZE)} limit")
        return {}
    report = fit_report(fit)
    size = fit.stat().st_size
    log(f"  {fit.name}: {human(size)}  sha256 {sha256_file(fit)}")
    for name, info in report["images"].items():
        log(f"    {name:<9} offset=0x{info['offset']:x} "
            f"size={info['size']:,} sha256_ok={info['hash_ok']}")
    log(f"    config={report['config']} default={report['default']} "
        f"signed={report['signed']}")

    # a DTS change shifts the kernel: state it explicitly
    fdt = report["images"].get("fdt", {})
    kern = report["images"].get("kernel", {})
    if fdt and kern:
        expected = fdt["offset"] + fdt["size"]
        if kern["offset"] != expected:
            log(f"    note: kernel offset 0x{kern['offset']:x} != "
                f"fdt end 0x{expected:x} (0x800 alignment applied)")

    if size > BOOT_SIZE:
        die(f"FIT is {human(size)} but p3 (boot) is {human(BOOT_SIZE)}: "
            f"does not fit - shrink the kernel/initramfs")
    head = BOOT_SIZE - size
    if head < 4 * 1024 * 1024:
        log(f"    WARNING: only {human(head)} headroom left on p3")
    log(f"    fits p3: {human(size)} of {human(BOOT_SIZE)}, "
        f"headroom {human(head)} ({head * 100.0 / BOOT_SIZE:.1f}%)")
    report.update({"size": size, "sha256": sha256_file(fit),
                   "boot_size": BOOT_SIZE, "headroom": head})
    return report


# --------------------------------------------------------------------------
# step: rootfs
# --------------------------------------------------------------------------
def ensure_tarball(args, R, env) -> Path:
    cache = Path(args.rootfs_cache).resolve()
    local = cache / UBUNTU_TARBALL
    legacy = cache / "ubuntu-base-arm64.tar.gz"
    expected = UBUNTU_SHA256

    sums = cache / "SHA256SUMS"
    if not sums.exists() and not R.dry:
        cache.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["curl", "-fsSL", "-o", str(sums),
                            f"{args.rootfs_url}/SHA256SUMS"], check=True)
        except subprocess.CalledProcessError:
            log(f"  [i] could not fetch upstream SHA256SUMS (offline?)")
            sums = None
    if sums is not None and sums.exists():
        for line in sums.read_text().splitlines():
            if line.endswith(UBUNTU_TARBALL):
                expected = line.split()[0]
                break

    if args.rootfs_tarball:
        t = Path(args.rootfs_tarball).resolve()
        if not t.exists():
            die(f"--rootfs-tarball {t} does not exist")
        got = sha256_file(t)
        if got != expected:
            die(f"--rootfs-tarball {t} sha256 {got} != expected {expected}")
        log(f"  using {t} (sha256 {got} verified)")
        return t

    for cand in (local, legacy):
        if cand.exists():
            got = sha256_file(cand)
            if got == expected:
                log(f"  reusing {cand} (sha256 {got} verified)")
                return cand
            log(f"  [!] {cand} sha256 {got} != expected {expected}")
            if not args.force_download:
                die(f"{cand} is not the expected image; "
                    "re-run with --force-download to replace it")
    if R.dry:
        log(f"  [dry] would download {args.rootfs_url}/{UBUNTU_TARBALL}")
        return local

    cache.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(".part")
    log(f"  downloading {args.rootfs_url}/{UBUNTU_TARBALL}")
    R.run(["curl", "-fL", "--retry", "3", "-o", tmp,
           f"{args.rootfs_url}/{UBUNTU_TARBALL}"])
    got = sha256_file(tmp)
    if got != expected:
        die(f"downloaded tarball sha256 {got} != expected {expected}")
    tmp.replace(local)
    log(f"  {local} {human(local.stat().st_size)} sha256 {got} verified")
    return local


def step_rootfs(args, R, out: Path, env) -> None:
    log("-- rootfs: ubuntu-base arm64 -> ext4 image --")
    tarball = ensure_tarball(args, R, env)
    stage = out / "rootfs-stage"
    img = out / "rootfs.ext4"
    size = parse_size(args.rootfs_size)
    if size > ROOTFS_SIZE:
        die(f"rootfs image {human(size)} > p11 {human(ROOTFS_SIZE)}")
    blocks = size // 4096

    if args.with_modules:
        log("  building kernel modules (make modules)")
        R.run(make_base() + [f"-j{args.jobs}", "modules"], env=env)

    inner = [
        "set -e",
        f"rm -rf {shlex.quote(str(stage))}",
        f"mkdir -p {shlex.quote(str(stage))}",
        f"tar -xzf {shlex.quote(str(tarball))} -C {shlex.quote(str(stage))}",
    ]
    if args.with_modules:
        inner.append(
            "make -C " + shlex.quote(str(KERNEL_TREE))
            + (f" O={shlex.quote(str(BUILD_DIR))}" if BUILD_DIR is not None else "")
            + " ARCH=arm64 INSTALL_MOD_PATH=" + shlex.quote(str(stage))
            + " modules_install")
    inner += [
        # uniform root ownership inside the image (fakeroot-recordable)
        f"chown -R 0:0 {shlex.quote(str(stage))}",
        f"mke2fs -q -t ext4 -F -L {shlex.quote(ROOTFS_LABEL)} -b 4096 -m 1 "
        f"-d {shlex.quote(str(stage))} {shlex.quote(str(img))} {blocks}",
    ]
    if R.dry:
        log(f"  [dry] would extract {tarball.name} into {stage}")
        log(f"  [dry] would build {img} ({human(size)}, {blocks} blocks)")
        return
    out.mkdir(parents=True, exist_ok=True)
    R.run(["fakeroot", "bash", "-c", "\n".join(inner)], env=env)

    # prove the image is well formed and owned by root
    fsck = subprocess.run(["e2fsck", "-fn", str(img)],
                          capture_output=True, text=True)
    if fsck.returncode != 0:
        die(f"e2fsck -fn {img} rc={fsck.returncode}\n{fsck.stdout}{fsck.stderr}")

    def dbg(query: str) -> str:
        return subprocess.run(["debugfs", "-R", query, str(img)],
                              capture_output=True, text=True).stdout

    m = re.search(r"([\d,]+)/([\d,]+) blocks", fsck.stdout)
    if m:
        used, total = (int(x.replace(",", "")) for x in m.groups())
        LAST["rootfs_fs"] = {"used_blocks": used, "total_blocks": total,
                             "used_percent": round(used * 100.0 / total, 2)}
        log(f"  ext4 usage: {used:,}/{total:,} blocks "
            f"({LAST['rootfs_fs']['used_percent']}% of the image)")

    rows = [ln.split() for ln in dbg("ls -l /").splitlines() if ln.strip()]
    nonroot = [r[-1] for r in rows if len(r) >= 5 and r[3:5] != ["0", "0"]]
    probe = dbg("stat /usr/bin/passwd")
    probe_ids = None
    for ln in probe.splitlines():
        if ln.startswith("User:"):
            probe_ids = (ln.split()[1], ln.split()[3])
    log(f"  e2fsck: clean (rc=0)")
    log(f"  / listing: {len(rows)} entries, "
        f"{len(rows) - len(nonroot)} owned by uid/gid 0"
        + (f", NOT root: {nonroot}" if nonroot else ""))
    log(f"  /usr/bin/passwd: uid/gid {probe_ids} (setuid bits survive)"
        if probe_ids else "  [!] /usr/bin/passwd not found in the image")
    if nonroot or probe_ids != ("0", "0"):
        die("rootfs image has non-root ownership; check the fakeroot step")
    st = img.stat()
    log(f"  {img.name}: {human(st.st_size)}  sha256 {sha256_file(img)}")
    du = subprocess.run(["du", "-h", "--apparent-size", str(img)],
                        capture_output=True, text=True).stdout.split()[0]
    log(f"  staged tree: {stage}  image apparent size {du}")


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


def write_manifest(out: Path, steps, env, fit_info, artifacts) -> Path:
    tree = KERNEL_TREE
    cfg = cfg_path()
    git = subprocess.run(["git", "-C", str(tree), "describe", "--tags",
                          "--always", "--dirty"],
                         capture_output=True, text=True).stdout.strip()
    initramfs = ""
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.startswith("CONFIG_INITRAMFS_SOURCE="):
                initramfs = line.split("=", 1)[1].strip().strip('"')
    cc = subprocess.run([env["CROSS_COMPILE"] + "gcc", "--version"],
                        env=env, capture_output=True, text=True
                        ).stdout.splitlines()[0]
    manifest = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": str(Path(__file__).resolve()),
        "steps": list(steps),
        "inputs": {
            "kernel_tree": str(tree),
        "kernel_tree_source": str(PRIMARY_TREE),
            "kernel_git": git,
            "kernel_config": str(cfg),
            "kernel_config_sha256": sha256_file(cfg) if cfg.exists() else None,
            "board_dts": str(tree / "arch/arm64/boot/dts/rockchip/rk3568-t2.dts"),
            "board_dts_sha256": sha256_file(
                tree / "arch/arm64/boot/dts/rockchip/rk3568-t2.dts")
            if (tree / "arch/arm64/boot/dts/rockchip/rk3568-t2.dts").exists()
            else None,
            "initramfs_source": initramfs or None,
            "toolchain": cc,
            "cross_compile": env["CROSS_COMPILE"],
            "fit_builder": str(RK_FIT),
        },
        "artifacts": artifacts,
        "fit": fit_info or None,
        "flash": {},
    }
    if fit_info:
        manifest["flash"]["boot_p3 (fit)"] = flash_plan(
            fit_info["size"], BOOT_LBA, BOOT_SIZE)
        manifest["flash"]["boot_p3 (fit)"]["writes"] = "t2-mainline-boot.img"
    if (out / "rootfs.ext4").exists():
        manifest["flash"]["rootfs_p11 (ext4)"] = flash_plan(
            (out / "rootfs.ext4").stat().st_size, ROOTFS_LBA, ROOTFS_SIZE)
        manifest["flash"]["rootfs_p11 (ext4)"]["writes"] = "rootfs.ext4"
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")

    sums = out / "SHA256SUMS"
    lines = [f"{a['sha256']}  {Path(a['path']).name}"
             for a in artifacts if Path(a['path']).parent == out]
    if lines:
        sums.write_text("\n".join(lines) + "\n")
    log(f"  manifest: {mpath}")
    if lines:
        log(f"  sums:     {sums}")
    return mpath


# --------------------------------------------------------------------------
def main() -> int:
    global KERNEL_TREE, BUILD_DIR, PRIMARY_TREE   # CLI overrides them
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--all", action="store_true",
                      help="kernel + fit + rootfs (default)")
    what.add_argument("--kernel-only", action="store_true",
                      help="kernel Image + DTB + FIT")
    what.add_argument("--rootfs-only", action="store_true",
                      help="rootfs tarball + stage + ext4 image")
    ap.add_argument("--steps", help="explicit comma list of: "
                    + ",".join(STEPS))
    ap.add_argument("--out-dir", default=str(OUT_DIR),
                    help=f"artifact output directory (default {OUT_DIR})")
    ap.add_argument("--kernel-tree", default=str(KERNEL_TREE))
    ap.add_argument("-O", "--build-dir", default="none",
                    help="kernel build output: a directory (make O=<dir>) or "
                         "'in-tree' to write into the source tree (the default, "
                         "matching kernel/build.sh)")
    ap.add_argument("--sync-tree", nargs="?", const=str(SYNC_TREE),
                    default=str(SYNC_TREE), metavar="DIR",
                    help="build in a private checkout of the source tree "
                         "(default: %(const)s).  The kernel refuses `make O=` "
                         "while the source tree has in-tree build state, so a "
                         "separate checkout is the only way to stay isolated "
                         "from somebody else building the primary tree")
    ap.add_argument("--no-sync-tree", action="store_true",
                    help="build in the primary tree like the original manual "
                         "flow (do not do this while another make is running "
                         "there: concurrent makes corrupt each other's "
                         "archives)")
    ap.add_argument("--wait-lock", action="store_true",
                    help="wait for the build lock instead of failing")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                    help="parallel make jobs (the manual flow used 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every command, write nothing")
    ap.add_argument("--bootstrap-config", action="store_true",
                    help="fresh checkout: make defconfig + documented options")
    ap.add_argument("--check-config", action="store_true",
                    help="verify the curated option list against the tree "
                         ".config, then exit")
    ap.add_argument("--trim-config", action="store_true",
                    help="apply the audited trim list to the tree's .config "
                         "(other SoC platforms, ACPI, KVM/Xen/virtio, SCSI "
                         "HBAs, ATA, TV/DVB, other-SoC USB hosts, the "
                         "non-fittable module wall) + MODULE_COMPRESS_ZSTD, "
                         "then olddefconfig and --check-config")
    ap.add_argument("--rootfs-size", default=str(ROOTFS_SIZE),
                    help="ext4 image size (bytes, or K/M/G suffix; "
                         "'p11' = partition size=%d)" % ROOTFS_SIZE)
    ap.add_argument("--rootfs-tarball", help="use this tarball (sha256 checked)")
    ap.add_argument("--rootfs-cache", default=str(ROOTFS_CACHE))
    ap.add_argument("--rootfs-url", default=UBUNTU_BASE_URL)
    ap.add_argument("--force-download", action="store_true",
                    help="replace a cached tarball whose hash does not match")
    ap.add_argument("--with-modules", action="store_true",
                    help="also build+install kernel modules into the rootfs "
                         "(slow: the tree has ~1470 =m symbols)")
    ap.add_argument("--flash", action="store_true",
                    help="after building, run tools/t2-flash.py on the FIT "
                         "(BOARD ACCESS - not allowed while another process "
                         "owns the board)")
    args = ap.parse_args()

    KERNEL_TREE = Path(args.kernel_tree).resolve()
    PRIMARY_TREE = KERNEL_TREE
    if args.no_sync_tree:
        args.sync_tree = None
    spec = args.build_dir.strip()
    BUILD_DIR = (None if spec in ("", "-", "in-tree", "none")
                 else Path(spec).resolve())
    if args.trim_config:
        apply_trim()
        check_config(args)   # the trim must leave every load-bearing symbol
    if args.check_config:
        check_config(args)

    if args.all:
        steps = list(STEPS)
    elif args.kernel_only:
        steps = ["kernel", "fit"]
    elif args.rootfs_only:
        steps = ["rootfs"]
    elif args.steps:
        steps = [s.strip() for s in args.steps.split(",") if s.strip()]
        bad = [s for s in steps if s not in STEPS]
        if bad:
            die(f"unknown step(s) {bad}; valid: {STEPS}")
    else:
        steps = list(STEPS)

    out = Path(args.out_dir).resolve()
    env = build_env()
    R = Runner(args.dry_run)
    log(f"== t2-build: steps={','.join(steps)} "
        f"{'[DRY RUN]' if args.dry_run else ''} ==")
    log(f"   source tree : {KERNEL_TREE}")
    log(f"   output dir  : {out}")
    lock = None
    if ({"kernel", "fit"} & set(steps)) and not args.dry_run:
        lock = acquire_lock(args)
    if args.sync_tree and "kernel" in steps:
        log("-- isolating the build in a private source tree --")
        KERNEL_TREE = sync_tree(args, R, env)
        BUILD_DIR = None        # private tree -> plain in-tree build
    if "kernel" in steps or "fit" in steps:
        log(f"   build root  : {build_root()}"
            + (" [in-tree]" if BUILD_DIR is None else " [O=]"))
    preflight(args, steps, env)
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    artifacts, fit_info = [], None
    if "kernel" in steps:
        step_kernel(args, R, out, env)
    if "fit" in steps:
        fit_info = step_fit(args, R, out, {})
    if "rootfs" in steps:
        step_rootfs(args, R, out, env)

    if args.dry_run:
        log("\n[dry run] no files were written")
        return 0

    # manifest + sums from what is actually on disk
    if (out / "Image").exists() or (out / "t2-mainline-boot.img").exists():
        for n in ("Image", "rk3568-t2.dtb", "t2-mainline-boot.img"):
            if (out / n).exists():
                artifacts.append(artifact(out / n, n))
    if (out / "rootfs.ext4").exists():
        artifacts.append(artifact(out / "rootfs.ext4", "rootfs.ext4",
                                  partition="p11 source_rootfs",
                                  partition_lba=hex(ROOTFS_LBA),
                                  partition_size=ROOTFS_SIZE,
                                  partition_fill_percent=round(
                                      (out / "rootfs.ext4").stat().st_size
                                      * 100.0 / ROOTFS_SIZE, 2),
                                  **LAST.get("rootfs_fs", {})))
    if "rootfs" in steps:
        t = ensure_tarball(args, Runner(True), env)
        if Path(t).exists():
            artifacts.append(artifact(Path(t), Path(t).name,
                                      source=f"{args.rootfs_url}/{UBUNTU_TARBALL}"))
    mpath = write_manifest(out, steps, env, fit_info, artifacts)

    log("\n== summary ==")
    for a in artifacts:
        log(f"  {a['name']:<24} {human(a['size'])}  sha256 {a['sha256']}")
    if fit_info:
        log(f"  FIT p3 usage: {human(fit_info['size'])} of "
            f"{human(BOOT_SIZE)} ({100.0 * fit_info['size'] / BOOT_SIZE:.1f}%), "
            f"headroom {human(fit_info['headroom'])}")
    log(f"  manifest: {mpath}")
    if args.flash:
        if "fit" not in steps:
            die("--flash needs the fit step")
        log("== flashing FIT to p3 via tools/t2-flash.py ==")
        subprocess.run([sys.executable, T2_FLASH,
                        "--image", str(out / "t2-mainline-boot.img")],
                       check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
