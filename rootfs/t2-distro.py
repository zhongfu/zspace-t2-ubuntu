#!/usr/bin/env python3
"""t2-build the distro half: a profile -> a flashable rootfs.ext4.

    rootfs/t2-distro.py --profile rootfs/profiles/t2-base \
                        --out build/rootfs

The profile is data (see rootfs/README.md); this driver turns it into an
ext4 image without host root and without a board:

    base      the cached, sha256-pinned base tarball, extracted into <out>/stage
    tools     qemu-user-static + proot, fetched unprivileged, then *proved*
    packages  policy-rc.d + apt-get update && apt-get install, inside the chroot
    overlay   overlay/** copied verbatim into the stage
    debs      build the board userspace .deb, ship it in the image's own apt
              repo (/opt/t2/repo) and install it from there in the chroot
    hooks     hooks/NN-*.sh, ascending, inside the chroot
    modules   the FIT kernel's modules, installed into the stage
    image     fakeroot + mke2fs -d -> <out>/<artifact>
    verify    file-based proof of the finished image (never a boot)
    manifest  manifest.json + SHA256SUMS

Stages that modify the stage carry a stamp under <out>/stage/.t2-stamps/ (the
base tarball sha, the package selection sha, the overlay tree hash, the hooks
hash, the kernel config/release).  A rerun skips a stage whose stamp matches,
and re-runs every later stage that consumed a rebuilt one; changing the base
tarball wipes the whole stage, stamps included.  The stamps are lifted out
around mke2fs so they never ship in the image.

Reused from the repo's shared pipeline code, lib/t2-build.py (loaded with
importlib - the name has a hyphen): Runner, build_env, sha256_file,
parse_size, artifact, flash_plan.  lib/rkimg.py supplies the FIT/DTS parsing
for the shipped-FIT check.  Both live once at <repo>/lib/ and are found from
this script's own location, so the tree works from any clone path.
The kernel/FIT half stays in t2-build.py; this driver never touches it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# the shared pipeline code (t2-build.py) and this repo's fixed facts
# --------------------------------------------------------------------------
SCRIPTS = Path(__file__).resolve().parent        # <repo>/rootfs
ROOT = SCRIPTS.parent                            # <repo>
LIB = ROOT / "lib"                               # shared pipeline code, once
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
import rkimg  # noqa: E402  (FIT/DTS parsing for the shipped-FIT check)


def _load_t2_build():
    spec = importlib.util.spec_from_file_location("t2_build",
                                                  LIB / "t2-build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T2B = _load_t2_build()
Runner = T2B.Runner            # prints every command, honours --dry-run
build_env = T2B.build_env      # host environment the manual build used
sha256_file = T2B.sha256_file
parse_size = T2B.parse_size
artifact = T2B.artifact        # {name, path, size, sha256}
flash_plan = T2B.flash_plan    # rockusb `wl` chunk arithmetic

STAGES = ("base", "tools", "packages", "overlay", "debs", "hooks", "modules",
          "image", "verify", "manifest")

# Unprivileged tool fetch.  `apt-get download` + `dpkg-deb -x` needs no root
# and is the only way to get qemu-aarch64-static here (no binfmt_misc: that
# needs root, and `sudo -n` fails on this host).
QEMU_DEBS = ("qemu-user-static",)
QEMU_BIN = "usr/bin/qemu-aarch64-static"

# proot: NOT taken from the Ubuntu archive.  The archive's proot 5.1.0
# (2018) does not translate guest paths when it runs a foreign binary
# through -q on this host: inside the chroot, open()/stat() then hit the
# *host* filesystem (proving it: `stat /etc/hostname` inside the chroot
# reports the host's inode, and every file the build creates is
# unreachable afterwards - which is also why apt's InRelease signature
# check fails with a gpg keydb EACCES).  The upstream static build
# translates correctly, so it is pinned here by sha256 instead - upstream
# replaces that file in place, so the pin has to be refreshed when the
# download no longer matches (the build served since 2026-10-02 is
# v5.4.1-32-g25dc6a3).  probe_chroot() below is what actually guards the path
# translation; this pin only records which binary was measured.
PROOT_URL = "https://proot.gitlab.io/proot/bin/proot"
PROOT_SHA256 = "90375de3807212b8f948ff98ed66020f7c9cf7ea447c8734f1af02a2643c8d26"

# Inside the chroot.  The base tarball ships no /etc/resolv.conf at all, so
# without this bind apt cannot resolve archive.ubuntu.com; the bind is
# transient and never lands in the image.
CHROOT_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
RESOLV_CONF = Path("/etc/resolv.conf")

# The profile's inputs that hooks read through the read-only /t2-profile
# bind (image.json: firmware_src is a repo-root relative path).
PROFILE_MNT = "/t2-profile"
# The board userspace ships as a real Debian package.  rootfs/packages/<name>/
# is its source: the payload tree under root/ plus DEBIAN metadata and the
# build.sh that assembles the .deb.  The driver builds it, drops the .deb into
# a flat apt repo *inside the image* (REPO_DIR) and installs it from that repo,
# so the build exercises the same offline `apt-get install/upgrade` a running
# board uses.
PACKAGE = "t2-utils"
PACKAGE_DIR = SCRIPTS / "packages" / PACKAGE
REPO_DIR = "/opt/t2/repo"
# Per-stage freshness stamps, kept *inside* <out>/stage so that wiping the
# stage (a changed base tarball) also drops them and forces every stage to
# re-run.  They are build metadata, not image content, so the image stage lifts
# them out around mke2fs and puts them back afterwards.
STAMP_DIR = ".t2-stamps"
# enablement the image must carry; verified as symlinks under
# /etc/systemd/system/*.wants/ (file-based, never started - see
# distro/README.md).
REQUIRED_UNITS = (
    "ssh.service",
    "NetworkManager.service",
    "systemd-resolved.service",
    "systemd-timesyncd.service",
    "avahi-daemon.service",
    "t2-leds.service",
    "t2-powerkey.service",
    "getty@tty1.service",
    "t2-growroot.service",
    "t2-hddled.service",
    "t2-ssh-hostkeys.service",
    "serial-getty@ttyS2.service",
    "t2-firstboot-identity.service",
    "t2-boot-commit.service",
    "t2-provision.service",
    "t2-usbgadget.service",
    "dnsmasq.service",
    "bluetooth.service",
    "t2-ble.service",
)
REQUIRED_PATHS = (
    "/usr/lib/systemd/systemd",
    "/etc/machine-id",
    "/usr/local/sbin/t2-firstboot-identity.sh",
    "/usr/local/sbin/t2-boot-commit.sh",
    "/etc/fw_env.config",
    "/usr/local/sbin/t2-hddled.sh",
    "/usr/local/sbin/t2-provision.sh",
    "/usr/local/sbin/t2-usbgadget.sh",
    "/usr/local/sbin/t2-powerkey.py",
    "/etc/t2/powerkey.conf",
    "/etc/systemd/logind.conf.d/10-t2.conf",
    "/etc/dnsmasq.d/t2-usbgadget.conf",
    "/etc/NetworkManager/conf.d/10-t2-manage-ethernet.conf",
    "/etc/NetworkManager/conf.d/20-t2-usb-gadget.conf",
    "/lib/firmware/brcm/brcmfmac43752-pcie.bin",
    "/lib/firmware/rtl_nic/rtl8156b-2.fw",
    "/usr/local/sbin/t2-ble.py",
    "/usr/local/sbin/t2-ble-password",
)

# The kernel the image's modules must match.  kernel/fetch.sh clones the
# patched Linux tree here and kernel/build.sh builds it, so `uname -r` on the
# board is this tree's `kernelrelease`.
DEFAULT_KERNEL_TREE = ROOT / "build/kernel"

# The board needs these from the FIT kernel (spelled as modules.dep spells
# them).  The NVMe *core* and the RK809 PMIC power key are built-in
# (CONFIG_NVME_CORE=y, CONFIG_INPUT_RK805_PWRKEY=y) - the pwrkey in
# particular because the flash-mode initramfs ships no /lib/modules, so a
# modular driver would leave the installer without its power button.
REQUIRED_MODULES = ("dw-hdmi-i2s-audio", "anx7625")
FORBIDDEN_MODULES = ("drivers/nvme/host/nvme.ko",
                     "drivers/nvme/host/nvme-core.ko",
                     "drivers/nvme/host/nvme-fabrics.ko",
                     "drivers/input/misc/rk805-pwrkey.ko")

LAST: dict = {}                # what the stages learn, for the manifest


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    print(f"t2-distro: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def short(p: Path) -> str:
    """Path relative to the repo when it is inside it, absolute otherwise.

    Profiles are declared inside the repo, but the test suite builds throwaway
    ones in /tmp; relative_to() would raise on those.
    """
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def human(n: int) -> str:
    return f"{n:,} B ({n / 2**20:.1f} MiB)"


# --------------------------------------------------------------------------
# per-stage freshness stamps
# --------------------------------------------------------------------------
def tree_hash(roots) -> str:
    """sha256 over a sorted (path, content) listing of `roots`.

    Content only: touching a file's mtime leaves the hash alone.  Symlinks
    contribute their target, so a retargeted link counts as a change.
    """
    entries = []
    for root in roots:
        root = Path(root)
        if root.is_symlink():
            entries.append((str(root), "link", os.readlink(root)))
        elif root.is_file():
            entries.append((str(root), "file", sha256_file(root)))
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_symlink():
                    entries.append((str(p.relative_to(root)), "link",
                                    os.readlink(p)))
                elif p.is_file():
                    entries.append((str(p.relative_to(root)), "file",
                                    sha256_file(p)))
    h = hashlib.sha256()
    for rel, kind, val in sorted(entries):
        h.update(f"{rel}\0{kind}\0{val}\0".encode())
    return h.hexdigest()


def ko_hash(tree: Path) -> str:
    """sha256 over a kernel tree's built modules (content, sorted).

    Used by the modules stage: a kernel patch edit leaves `.config` and the
    release string (`git describe --dirty`) identical, so the built .ko are the
    only thing that shows the tree changed.
    """
    return tree_hash([p for p in tree.rglob("*.ko") if p.is_file()])


def modules_stamp(tree: Path, rel: str) -> str:
    """The modules stage's freshness stamp: config, release, tree, built .ko.

    A kernel patch edit leaves `.config` and the release string (`git describe
    --dirty`) identical, so the built modules are the only thing that shows the
    tree changed - without them the stage would ship stale .ko into the image.
    """
    return f"{sha256_file(tree / '.config')} {rel} {tree} {ko_hash(tree)}"


def stamp_path(stage: Path, name: str) -> Path:
    return stage / STAMP_DIR / name


def stamp_read(stage: Path, name: str) -> str:
    p = stamp_path(stage, name)
    try:
        return p.read_text().strip() if p.is_file() else None
    except OSError:
        return None


def stamp_write(stage: Path, name: str, value: str) -> None:
    p = stamp_path(stage, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")


def prior_manifest(out: Path) -> dict:
    """The last finished build's manifest, or {} when there is none.

    A stage built before stamping existed carries no stamp; the manifest is
    that build's own record of the base tarball and the package selection, so
    a matching stage can be adopted instead of re-extracted and re-apt.
    """
    p = out / "manifest.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def overlay_hash(prof: "Profile") -> str:
    """Hash of every overlay layer's tree (content + symlink targets)."""
    return tree_hash(prof.overlays or [])


def hooks_hash(prof: "Profile") -> str:
    """Hash of the hooks that will run and the inputs they read.

    Every layer's hook body (the deepest layer wins a shared name), the hook
    environment, and the firmware tree image.json points at - hook 50 copies
    it into /lib/firmware.  A hook's *result* also depends on the packages and
    the overlay that ran before it, so the driver re-runs hooks whenever either
    of those stages rebuilt.
    """
    h = hashlib.sha256()
    for name in prof.hooks:
        for layer in reversed(prof.layers):
            f = layer / "hooks" / name
            if f.is_file():
                h.update(f"{name}\0".encode() + f.read_bytes() + b"\0")
                break
    h.update(json.dumps(prof.hook_env(), sort_keys=True).encode() + b"\0")
    src = prof.image.get("firmware_src")
    if src:
        fw = (ROOT / src).resolve()
        h.update(tree_hash([fw] if fw.is_dir() else []).encode() + b"\0")
    return h.hexdigest()


def base_facts(args, prof: Profile, tarball: Path = None) -> None:
    if tarball is None:
        cache = Path(args.base_cache).resolve()
        tarball = cache / Path(urlparse(prof.base["url"]).path).name
    LAST["base"] = {"url": prof.base["url"], "sha256": prof.base["sha256"],
                    "tarball": str(tarball),
                    "distro": prof.base.get("distro"),
                    "release": prof.base.get("release"),
                    "arch": prof.base.get("arch")}


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------
def _json_at(d: Path, name: str, required: bool = True) -> dict:
    p = d / name
    if not p.exists():
        if not required:
            return {}
        die(f"{p} is missing")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"{p}: {e}")


def _packages_at(d: Path) -> list:
    p = d / "packages.txt"
    if not p.exists():
        die(f"{p} is missing")
    out = []
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _hooks_at(d: Path) -> list:
    return [p.name for p in (d / "hooks").glob("*.sh") if p.name[:2].isdigit()]


def _resolve_ext(d: Path, ext: str) -> Path:
    """`extends` is the name of a sibling under rootfs/profiles/, or a path
    relative to the profile (both are tried)."""
    for cand in (d / ext, d.parent / ext):
        if cand.is_dir():
            return cand.resolve()
    die(f"{d}/base.json: extends {ext!r} matches neither {d / ext} "
        f"nor {d.parent / ext}")


def deep_merge(base: dict, child: dict) -> dict:
    """Merge `child` over `base`, recursing into nested dicts.

    Sections like image.json's `root`/`fit`/`partition` are objects, so a NAS
    profile that only changes `label` must not have to restate the parent's
    root spec (which is what a flat update would silently erase).
    """
    out = dict(base)
    for k, v in child.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def dedup(seq: list) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def sha256_files(paths) -> str:
    """sha256 over the concatenation of `paths`.

    A profile can carry one packages.txt per layer; for a single file this is
    exactly sha256_file(), so manifests of unlayered profiles are unchanged.
    """
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()


class Profile:
    """The declarative input; see distro/README.md for the schema.

    A profile may declare `"extends": "<sibling name or relative path>"` in
    base.json.  Layers then resolve root-first and merge:

      * base.json / image.json - shallow, child key wins
      * packages.txt          - concatenated parent-first, duplicates dropped
      * hooks/*.sh            - union ascending by numeric prefix; on a numeric
                                tie the parent's runs first, and a child file
                                with the parent's name *replaces* it outright
      * overlay/**            - copied parent-first, child files overwrite

    Nothing is inherited implicitly: an extending profile still ships its own
    base.json (which may be nothing but `{"extends": ...}`), packages.txt and
    hooks/.
    """

    def __init__(self, path: Path) -> None:
        self.dir = path.resolve()
        if not self.dir.is_dir():
            die(f"--profile {path} is not a directory")
        self.layers = self._resolve_layers(self.dir)
        self.base, self.image = {}, {}
        for d in self.layers:
            self.base = deep_merge(self.base, _json_at(d, "base.json"))
            self.image = deep_merge(self.image, _json_at(d, "image.json"))
        self.packages = dedup(
            [sel for d in self.layers for sel in _packages_at(d)])
        if not self.packages:
            die(f"{self.dir}: the profile selects no packages")
        order = {}                     # hook name -> layer index, later wins
        for i, d in enumerate(self.layers):
            for name in _hooks_at(d):
                order[name] = i
        self.hooks = sorted(order, key=lambda n: (int(n[:2]), order[n]))
        self.overlays = [d / "overlay" for d in self.layers
                         if (d / "overlay").is_dir()]
        self.overlay = self.dir / "overlay"
        self.name = self.base.get("name") or self.dir.name
        self.extends = self.base.get("extends")

    @property
    def layered(self) -> bool:
        return len(self.layers) > 1

    @property
    def units(self) -> list:
        """Units this profile itself requires enabled (image.json `units`).

        The driver's REQUIRED_UNITS floor is checked for every profile; this is
        the profile's own additions, so a layered build proves its extra units
        are actually enabled rather than merely installed.  Without it the
        verification only ever reports the base profile's floor units, so a unit
        enabled by a hook goes unchecked.
        """
        return list(self.image.get("units") or [])

    @property
    def packages_files(self) -> list:
        return [d / "packages.txt" for d in self.layers]

    @property
    def packages_file(self) -> Path:
        """The profile's own selections (the child's file, when layered)."""
        return self.dir / "packages.txt"

    def _resolve_layers(self, d: Path, seen: set = None) -> list:
        seen = set() if seen is None else seen
        if d in seen:
            die(f"{d}: profile `extends` cycle")
        seen.add(d)
        ext = _json_at(d, "base.json", required=False).get("extends")
        if not ext:
            return [d]
        return self._resolve_layers(_resolve_ext(d, ext), seen) + [d]

    @property
    def artifact(self) -> str:
        return self.image.get("artifact", "rootfs.ext4")

    @property
    def label(self) -> str:
        return self.image.get("label", "rootfs")

    @property
    def size(self) -> int:
        return int(self.image.get("size", 0))

    @property
    def root(self) -> dict:
        return self.image.get("root", {})

    @property
    def fit(self) -> dict:
        return self.image.get("fit", {})

    @property
    def fs_uuid(self) -> str:
        """Filesystem UUID, when the profile pins it (reproducible builds).

        Fixed only because nothing addresses this filesystem by UUID - the
        profile's root spec uses LABEL (image.json `root.cmdline`), so a stable
        UUID costs nothing and makes two builds of the same tree bit-identical.
        """
        return self.image.get("fs_uuid", "")

    def hook_env(self) -> dict:
        """The variables the hooks are documented to see (distro/README.md).

        T2_ROOT_PASSWORD is the documented static root password for bring-up
        (hooks/40-console.sh); it never reaches the manifest or SHA256SUMS.
        """
        return {
            "T2_PROFILE_DIR": PROFILE_MNT,
            "T2_ROOT_PARTUUID": self.root.get("partuuid", ""),
            "T2_ROOT_FSTYPE": self.root.get("fstype", "ext4"),
            "T2_IMAGE_LABEL": self.label,
            "T2_ROOT_PASSWORD": "t2",
        }


# --------------------------------------------------------------------------
# chroot backends
# --------------------------------------------------------------------------
class Chroot:
    """Runs a POSIX sh script as the *target* architecture in <stage>.

    proot (default, no root): proot -0 -q qemu-aarch64-static -r <stage> ...
    `-0` is load-bearing: the profile's hooks install into /root and dpkg
    refuses to run unprivileged, so the guest has to believe it is root.
    Nothing is really chowned - the stage is re-owned under fakeroot when
    the image is built.
    sudo (opt-in): a real chroot with real bind mounts, ~10x faster, needs
    a password on this host and is therefore never the default.
    none: no chroot at all (overlay/image only; hooks and debs skip)
    """

    HOST_BINDS = ("/proc", "/dev", "/sys")

    def __init__(self, backend: str, stage: Path, binds: list, env: dict,
                 proot: Path = None, qemu: Path = None) -> None:
        self.backend = backend
        self.stage = stage
        self.binds = list(binds)
        self.env = dict(env)
        self.proot = proot
        self.qemu = qemu

    def argv(self, script: str) -> list:
        env = ["/usr/bin/env", "-i"]
        for k, v in self.env.items():
            env.append(f"{k}={v}")
        env += ["/bin/sh", "-c", script]
        if self.backend == "none":
            die("this stage needs a chroot backend (--backend proot|sudo)")
        if self.backend == "sudo":
            return ["sudo", "chroot", str(self.stage)] + env
        if self.proot is None or self.qemu is None:
            die("proot backend needs the tools stage (proot + qemu)")
        # /proc, /dev and /sys must be bound for the *proot* backend too: the
        # sudo backend gets them from mount(8), but under proot nothing else
        # provides them, and systemd's postinst fails ("/proc/ is not
        # mounted", then ENOSYS from its machine-id setup) without /proc.
        argv = [str(self.proot), "-0", "-q", str(self.qemu),
                "-r", str(self.stage)]
        for host in self.HOST_BINDS:
            argv += ["-b", host]
        for src, dst in self.binds:
            argv += ["-b", f"{src}:{dst}"]
        argv += ["-w", "/"]
        return argv + env

    def run(self, R: Runner, script: str, *, capture: bool = False):
        mounted = False
        if self.backend == "sudo" and not R.dry:
            self._mount(R)
            mounted = True
        try:
            return R.run(self.argv(script), capture=capture)
        finally:
            if mounted:
                self._umount(R)

    def _mount(self, R: Runner) -> None:
        for host in self.HOST_BINDS:
            R.run(["sudo", "mount", "--bind", host,
                   str(self.stage / host.lstrip("/"))])
        for src, dst in self.binds:
            R.run(["sudo", "mkdir", "-p", str(self.stage / dst.lstrip("/"))])
            R.run(["sudo", "mount", "--bind", src,
                   str(self.stage / dst.lstrip("/"))])

    def _umount(self, R: Runner) -> None:
        targets = [str(self.stage / dst.lstrip("/")) for _, dst in self.binds]
        targets += [str(self.stage / h.lstrip("/")) for h in self.HOST_BINDS]
        for t in reversed(targets):
            subprocess.run(["sudo", "umount", "-l", t], capture_output=True)


# --------------------------------------------------------------------------
# stage: base
# --------------------------------------------------------------------------
def ensure_tarball(args, R: Runner, prof: Profile) -> Path:
    """The pinned base tarball, cached and re-verified by sha256."""
    cache = Path(args.base_cache).resolve()
    name = Path(urlparse(prof.base["url"]).path).name
    local = cache / name
    want = prof.base["sha256"]

    if local.exists():
        got = sha256_file(local)
        if got == want:
            log(f"  cached {local.name} {human(local.stat().st_size)} "
                f"sha256 {got} verified")
            return local
        log(f"  [!] {local} sha256 {got} != {want}; re-fetching")

    if R.dry:
        log(f"  [dry] would download {prof.base['url']}")
        return local
    cache.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(local.suffix + ".part")
    R.run(["curl", "-fL", "--retry", "3", "-o", str(tmp), prof.base["url"]])
    got = sha256_file(tmp)
    if got != want:
        tmp.unlink(missing_ok=True)
        die(f"downloaded {name} sha256 {got} != {want}")
    tmp.replace(local)
    log(f"  {local} {human(local.stat().st_size)} sha256 {got} verified")
    return local


def stage_base(args, R: Runner, prof: Profile, stage: Path) -> bool:
    """Extract the pinned tarball; True when the stage tree was (re)written.

    A stage whose stamp matches the pinned tarball sha is left untouched.  A
    stage with no stamp that the last manifest recorded as built from this
    tarball is adopted (it predates stamping).  Anything else is wiped: a
    changed base tarball invalidates every later stage, so the whole tree is
    rebuilt from scratch.
    """
    log("-- base: pinned tarball -> stage tree --")
    tarball = ensure_tarball(args, R, prof)
    want = prof.base["sha256"]
    if R.dry:
        log(f"  [dry] would extract {tarball.name} into {stage} unless fresh")
        return False
    have = stamp_read(stage, "base")
    if have == want:
        log(f"  [skip] base: unchanged ({short(stamp_path(stage, 'base'))})")
        base_facts(args, prof, tarball)
        return False
    if stage.is_dir() and have is None \
            and (stage / "etc/os-release").is_file() \
            and (prior_manifest(stage.parent).get("base") or {}).get(
                "sha256") == want:
        log("  [adopt] base stamp from "
            f"{short(stage.parent / 'manifest.json')}: unchanged")
        stamp_write(stage, "base", want)
        base_facts(args, prof, tarball)
        return False
    if stage.exists():
        log(f"  base stamp missing or stale for {want[:12]}; "
            f"wiping {short(stage)}")
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # plain user extraction: ownership is fixed under fakeroot at image time
    R.run(["tar", "-xzf", str(tarball), "-C", str(stage)])
    stamp_write(stage, "base", want)
    base_facts(args, prof, tarball)
    return True


# --------------------------------------------------------------------------
# stage: tools
# --------------------------------------------------------------------------
def _dpkg_cache(args, R: Runner) -> tuple:
    cache = Path(args.tools_cache).resolve()
    return cache / "debs", cache / "root"



def cached_tools(args) -> tuple:
    """Already-fetched tool paths, or (None, ...).  No network, no writes.

    A --stages run that skips the tools stage still needs the binaries; this
    resolves them from the cache so such a run neither downloads nor fails.
    """
    cache = Path(args.tools_cache).resolve()
    proot, qemu = cache / "proot", cache / "root" / QEMU_BIN
    if proot.is_file() and sha256_file(proot) != PROOT_SHA256:
        proot = None
    return proot, (qemu if qemu.is_file() else None)

def ensure_qemu(args, R: Runner) -> Path:
    debs, root = _dpkg_cache(args, R)
    qemu = root / QEMU_BIN
    if qemu.exists():
        return qemu
    if R.dry:
        log(f"  [dry] would apt-get download {' '.join(QEMU_DEBS)}")
        return qemu
    debs.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    for name in QEMU_DEBS:
        R.run(["apt-get", "download", name], cwd=debs)
    for deb in sorted(debs.glob("*.deb")):
        R.run(["dpkg-deb", "-x", str(deb), str(root)])
    if not qemu.exists():
        die(f"{QEMU_BIN} missing after dpkg-deb -x of {QEMU_DEBS}")
    return qemu


def ensure_proot(args, R: Runner) -> Path:
    cache = Path(args.tools_cache).resolve()
    local = cache / "proot"
    if local.exists() and sha256_file(local) == PROOT_SHA256:
        return local
    if R.dry:
        log(f"  [dry] would download {PROOT_URL}")
        return local
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / "proot.part"
    R.run(["curl", "-fsSL", "-o", str(tmp), PROOT_URL])
    got = sha256_file(tmp)
    if got != PROOT_SHA256:
        tmp.unlink(missing_ok=True)
        die(f"{PROOT_URL} sha256 {got} != pinned {PROOT_SHA256}")
    tmp.replace(local)
    local.chmod(0o755)
    return local


def probe_chroot(R: Runner, ch: Chroot, stage: Path) -> dict:
    """Prove the backend before anything is built with it.

    Two things must hold: the guest really is arm64, and the guest's paths
    really land in <stage> (a proot that fails the second test silently
    reads and writes the *host* filesystem - see the PROOT_SHA256 note).
    """
    want_ino = (stage / "etc/hostname").stat().st_ino
    script = 'uname -m; stat -c %i /etc/hostname'
    out = ch.run(R, script, capture=True).stdout.split()
    arch, ino = (out + ["", ""])[:2]
    ok = arch == "aarch64" and ino == str(want_ino)
    log(f"  probe: uname -m -> {arch}; /etc/hostname inode -> {ino} "
        f"(stage {want_ino})")
    if not R.dry and not ok:
        die("chroot backend does not translate paths into the stage "
            f"(uname={arch!r}, inode={ino!r}, want {want_ino}); refusing to "
            "build - the host filesystem would be modified instead")
    return {"uname_m": arch, "etc_hostname_inode": ino,
            "stage_etc_hostname_inode": str(want_ino), "ok": ok}


def stage_tools(args, R: Runner, prof: Profile, stage: Path,
                chroots: list) -> None:
    log("-- tools: unprivileged qemu-user-static + proot, then prove them --")
    if args.backend == "none":
        log("  [skip] backend none: no chroot, no tools")
        return
    proot, qemu = ensure_proot(args, R), ensure_qemu(args, R)
    for c in chroots:          # the hooks stage runs with extra binds
        c.proot, c.qemu = proot, qemu
    ch = chroots[0]
    if R.dry:
        return
    tools = tool_facts(ch.proot, ch.qemu)
    log(f"  {ch.proot.name}: {tools['proot']['version']}")
    log(f"  {ch.qemu.name}: {tools['qemu_aarch64_static']['version']}")
    LAST["tools"] = tools
    LAST["probe"] = probe_chroot(R, ch, stage)


def tool_facts(proot: Path, qemu: Path) -> dict:
    """Path, pinned sha256 and version of the two chroot tools.

    Recorded from the binaries themselves, not remembered from the tools
    stage, so a --stages run that skips that stage still writes a manifest
    that says exactly what emulated the build.
    """
    pv = subprocess.run([str(proot), "--version"], capture_output=True,
                        text=True).stdout
    # the version sits at the end of the ASCII-art banner line
    m = re.search(r"v\d+\.\d+\S*", pv)
    qv = subprocess.run([str(qemu), "--version"], capture_output=True,
                        text=True).stdout.strip().splitlines()[0]
    return {"proot": {"path": str(proot), "sha256": sha256_file(proot),
                      "url": PROOT_URL,
                      "version": m.group(0) if m else ""},
            "qemu_aarch64_static": {"path": str(qemu),
                                    "sha256": sha256_file(qemu),
                                    "package": " ".join(QEMU_DEBS),
                                    "version": qv}}


# --------------------------------------------------------------------------
# stage: packages
# --------------------------------------------------------------------------
def stage_packages(args, R: Runner, prof: Profile, ch: Chroot) -> bool:
    """apt install the profile's selections; True when it ran.

    Skipped when the package selection's stamp matches - the install (35
    selections under proot/qemu) is the expensive half of a rootfs rebuild.
    """
    stage = ch.stage
    want = sha256_files(prof.packages_files)
    log(f"-- packages: {len(prof.packages)} selections in the chroot --")
    if args.backend == "none":
        log("  [skip] backend none: nothing to install")
        return False
    if R.dry:
        log("  [dry] would write usr/sbin/policy-rc.d (exit 101)")
        log(f"  [dry] would apt-get install {len(prof.packages)} selections")
        return False
    have = stamp_read(stage, "packages")
    adopt = have is None and packages_installed(stage, prof.packages) \
        and (prior_manifest(stage.parent).get("packages") or {}).get(
            "sha256") == want
    if have == want or adopt:
        if adopt:
            log("  [adopt] packages stamp from "
                f"{short(stage.parent / 'manifest.json')}: unchanged")
            stamp_write(stage, "packages", want)
        log(f"  [skip] packages: unchanged "
            f"({short(stamp_path(stage, 'packages'))})")
        LAST["packages"] = {"list": prof.packages, "sha256": want,
                            "count": len(prof.packages), "recommends": True}
        return False

    policy = stage / "usr/sbin/policy-rc.d"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("#!/bin/sh\n# build-time: never start a service "
                      "here\nexit 101\n")
    policy.chmod(0o755)
    log(f"  wrote {policy} (exit 101)")

    # -j buys parallel HTTP fetches; dpkg itself unpacks serially
    apt_opts = (f"-o Acquire::Queue-Mode=access "
                f"-o Acquire::http::Pipeline-Depth={args.jobs} ")

    install = (f"apt-get {apt_opts}install -y "
               "-o APT::Install-Recommends=true "
               + " ".join(shlex.quote(p) for p in prof.packages))
    ch.run(R, "set -e\napt-get update")
    ch.run(R, "set -e\n" + install)
    # Every selection must be fully configured, not merely unpacked: a
    # half-configured systemd is exactly the kind of damage an image that
    # looks fine on disk would hide.
    left = unfinished_packages(stage)
    if left:
        die(f"{len(left)} package(s) left unconfigured after the install: "
            + ", ".join(left[:10]))
    log(f"  all {len(prof.packages)} selections installed and configured")
    stamp_write(stage, "packages", want)
    LAST["packages"] = {"list": prof.packages, "sha256": want,
                        "count": len(prof.packages), "recommends": True}
    return True


def unfinished_packages(stage: Path) -> list:
    """dpkg selections left in anything but "install ok installed"."""
    status = stage / "var/lib/dpkg/status"
    out, name, ok = [], None, False
    for line in status.read_text(errors="replace").splitlines():
        if line.startswith("Package: "):
            if name and not ok:
                out.append(name)
            name, ok = line[9:].strip(), False
        elif line.startswith("Status: "):
            ok = line == "Status: install ok installed"
    if name and not ok:
        out.append(name)
    return out


def packages_installed(stage: Path, packages) -> bool:
    """True when every selection is "install ok installed" in the stage.

    Used to adopt a packages stamp from the previous manifest: a stage that was
    just re-extracted from the base tarball has no selections yet, so adoption
    must not skip the install.
    """
    status = stage / "var/lib/dpkg/status"
    if not status.is_file():
        return False
    want, found, name, ok = set(packages), set(), None, False
    for line in status.read_text(errors="replace").splitlines():
        if line.startswith("Package: "):
            if name in want and ok:
                found.add(name)
            name, ok = line[9:].strip(), False
        elif line.startswith("Status: "):
            ok = line == "Status: install ok installed"
    if name in want and ok:
        found.add(name)
    return found >= want


# --------------------------------------------------------------------------
# stage: overlay
# --------------------------------------------------------------------------
def stage_overlay(args, R: Runner, prof: Profile, stage: Path,
                  force: bool = False) -> bool:
    """Copy the profile overlay into the stage; True when it ran.

    Skipped when the overlay tree's hash matches and no earlier stage rebuilt
    the tree (`force`); a packages rebuild can overwrite overlay files, so the
    driver forces this stage whenever packages ran.
    """
    log("-- overlay: profile files, modes preserved --")
    # A profile can ship no overlay at all.  A *reused* stage may still hold
    # files an earlier overlay run wrote (the ledger), and those have to go -
    # returning early here would leave them in the stage and in every image
    # built from it.
    ledger = stage.parent / "overlay-files.txt"
    if not prof.overlays:
        pruned = prune_overlay(ledger, stage, set(), dry=R.dry) \
            if ledger.is_file() else []
        if not R.dry:
            ledger.write_text("")
            stamp_write(stage, "overlay", overlay_hash(prof))
        log("  [skip] no overlay/ in the profile"
            + (f"; pruned {len(pruned)} file(s) it no longer ships: "
               + ", ".join(pruned) if pruned else ""))
        LAST["overlay"] = {"source": str(prof.overlay), "files": 0,
                           "sources": [], "pruned": pruned}
        return False
    want = overlay_hash(prof)
    # cp -a only ever adds and overwrites, so on a *reused* stage a file the
    # profile has since deleted would survive silently and ship.  The ledger
    # of paths the previous overlay run wrote is what makes that detectable:
    # a stale file has no reason to share a name with a live one.
    # is_file() is False for a symlink, so such a path would be pruned as
    # "stale" immediately after being copied - symlinks are real content
    # (/etc/resolv.conf is shipped as one).
    shipped = set()
    for ov in prof.overlays:
        shipped |= {str(p.relative_to(ov)) for p in ov.rglob("*")
                    if p.is_file() or p.is_symlink()}
    if not R.dry and not force and stamp_read(stage, "overlay") == want:
        log(f"  [skip] overlay: unchanged "
            f"({short(stamp_path(stage, 'overlay'))})")
        LAST["overlay"] = {"source": str(prof.overlay), "files": len(shipped),
                           "sources": [str(o) for o in prof.overlays],
                           "pruned": []}
        return False
    pruned = prune_overlay(ledger, stage, shipped, dry=R.dry) \
        if ledger.is_file() else []
    if R.dry:
        for ov in prof.overlays:
            log(f"  [dry] would cp -a {ov}/. {stage}/")
        if pruned:
            log(f"  [dry] would prune {len(pruned)} stale overlay file(s): "
                + ", ".join(pruned))
        return False
    n = len(shipped)
    # parent layers first, so an extending profile's file always wins
    for ov in prof.overlays:
        R.run(["cp", "-a", str(ov) + "/.", str(stage) + "/"])
    ledger.write_text("\n".join(sorted(shipped)) + "\n")
    layers = len(prof.overlays)
    log(f"  copied {n} files from {layers} overlay layer(s)"
        + (" (" + ", ".join(str(o) for o in prof.overlays) + ")" if layers > 1
           else ""))
    if pruned:
        log(f"  pruned {len(pruned)} file(s) the profile no longer ships: "
            + ", ".join(pruned))
    stamp_write(stage, "overlay", want)
    LAST["overlay"] = {"source": str(prof.overlay), "files": n,
                       "sources": [str(o) for o in prof.overlays],
                       "pruned": pruned}
    return True


def prune_overlay(ledger: Path, stage: Path, shipped: set,
                  dry: bool = False) -> list:
    """Delete staged files an earlier overlay run wrote and this one no
    longer ships (`ledger` minus `shipped`).

    Scoped to the ledger on purpose: those paths are files this profile's own
    overlay put in the stage, so a package-owned file elsewhere in the tree
    cannot be reached even by accident.  `dry` only reports: a --dry-run must
    never mutate the stage.
    """
    stale = set(ledger.read_text().split()) - shipped
    pruned = []
    for rel in sorted(stale):
        target = stage / rel
        # a symlink is real overlay content (/etc/resolv.conf ships as one),
        # so it has to be pruned too - and is_file() follows it
        if not (target.is_file() or target.is_symlink()):
            continue
        pruned.append(rel)
        if dry:
            continue
        target.unlink()
        # a directory the overlay created only to hold this file must go
        # with it, or an emptied __pycache__ would still ship
        parent = target.parent
        while parent != stage and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return pruned


# --------------------------------------------------------------------------
# stage: debs
# --------------------------------------------------------------------------
def package_version(prof: Profile) -> str:
    """The t2-utils Debian version: the profile's base release, sanitised.

    The rootfs build already derives this string (base.json `release`; the
    manifest records it under `base`), and tying the package version to the
    distro release it was built against is the one version number the profile
    owns.  A Debian version may carry [0-9A-Za-z.+~], so anything else is
    dropped; a profile with no release falls back to the documented
    `0.1.0~<git describe --tags --always --dirty>`.
    """
    rel = re.sub(r"[^0-9A-Za-z.+~]", "", str(prof.base.get("release") or ""))
    if rel:
        return rel
    out = subprocess.run(
        ["git", "-C", str(ROOT), "describe", "--tags", "--always", "--dirty"],
        capture_output=True, text=True).stdout.strip() or "unknown"
    return "0.1.0~" + re.sub(r"[^0-9A-Za-z.+~]", "", out)


def write_repo(stage: Path, deb: Path) -> Path:
    """A flat apt repo inside the image: REPO_DIR plus its Packages index.

    Built by hand rather than with dpkg-scanpackages: the build image has no
    dpkg-dev, and one package needs one Packages entry.  The index fields come
    from the .deb's own control; the long description must stay last, or apt
    reads the Filename/size/hash lines as more description text (ordering is
    what dpkg-scanpackages produces).  `[trusted=yes]` in the source line lets
    apt use it without a Release signature - the repo ships the image's own
    package, not a third-party feed.
    """
    repo = stage / REPO_DIR.lstrip("/")
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    local = repo / deb.name
    shutil.copy2(deb, local)

    control = subprocess.run(["dpkg-deb", "-f", str(local)],
                             capture_output=True, text=True,
                             check=True).stdout.splitlines()
    idx = next(i for i, l in enumerate(control)
               if l.startswith("Description:"))
    head, desc = control[:idx], control[idx:]
    head += [f"Filename: ./{local.name}", f"Size: {local.stat().st_size}"]
    # a Packages entry spells the digest fields MD5sum and SHA256 (no "sum")
    for field, digest in (("MD5sum", hashlib.md5), ("SHA256", hashlib.sha256)):
        h = digest()
        with local.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        head.append(f"{field}: {h.hexdigest()}")
    (repo / "Packages").write_text("\n".join(head + desc) + "\n\n")

    src = stage / "etc/apt/sources.list.d/t2.list"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(f"deb [trusted=yes] file:{REPO_DIR} ./\n")
    return local


def deb_facts(local: Path, version: str) -> dict:
    return {"package": PACKAGE, "version": version,
            "deb": f"{REPO_DIR}/{local.name}", "sha256": sha256_file(local),
            "repo": REPO_DIR, "installed": True}


def stage_debs(args, R: Runner, prof: Profile, stage: Path, ch: Chroot,
               force: bool = False) -> bool:
    """Build t2-utils, ship it in the image's apt repo, install it from there.

    The package now provides the board files the profile overlay used to copy
    verbatim, so this stage replaces that copy: it is the same content at the
    same paths, owned by dpkg.  Installing from the file: repo is deliberate -
    the path a running board takes (`apt-get install/upgrade t2-utils`) is what
    the build exercises, and the repo it reads ships in the image.

    `force` re-installs even when the stamp matches: the overlay stage prunes
    the paths the package now owns, so a run that rebuilt the overlay (or the
    packages under it) must put them back.
    """
    version = package_version(prof)
    want = f"{PACKAGE} {version} {tree_hash([PACKAGE_DIR])}"
    repo = stage / REPO_DIR.lstrip("/")
    local = repo / f"{PACKAGE}_{version}_all.deb"
    log(f"-- debs: build {PACKAGE} {version}, assemble {REPO_DIR}, apt install "
        "--")
    if args.backend == "none":
        log("  [skip] backend none: cannot install in the chroot")
        LAST["debs"] = {"package": PACKAGE, "version": version,
                        "installed": False}
        return False
    if R.dry:
        log(f"  [dry] would build {PACKAGE}_{version}_all.deb and install it "
            f"from {REPO_DIR}")
        return False
    if not force and stamp_read(stage, "debs") == want and local.is_file() \
            and (stage / "var/lib/dpkg/info" / f"{PACKAGE}.list").is_file():
        log(f"  [skip] debs: unchanged "
            f"({short(stamp_path(stage, 'debs'))})")
        LAST["debs"] = deb_facts(local, version)
        return False

    deb = args.out.resolve() / "repo" / f"{PACKAGE}_{version}_all.deb"
    deb.parent.mkdir(parents=True, exist_ok=True)
    R.run([str(PACKAGE_DIR / "build.sh"), version, str(deb)])
    local = write_repo(stage, deb)
    log(f"  {short(local)} ({local.stat().st_size:,} B)")
    LAST["debs"] = deb_facts(local, version)

    # Install with NO /etc/resolv.conf bind: the source is file:, so apt needs
    # no DNS, and the package ships /etc/resolv.conf itself - dpkg cannot
    # replace a file the chroot bind-mounted over it (proot refuses to unlink a
    # bind source).
    inst = Chroot(args.backend, stage, [], CHROOT_ENV | prof.hook_env(),
                  proot=ch.proot, qemu=ch.qemu)
    apt_opts = ("-o Dir::Etc::sourcelist=sources.list.d/t2.list "
                "-o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 "
                "-o Acquire::Languages=none")
    inst.run(R, "set -e\napt-get update " + apt_opts)
    # --reinstall, so a rebuilt .deb with the same version still lands
    inst.run(R, f"set -e\napt-get install -y --reinstall {PACKAGE}")
    if not (stage / "var/lib/dpkg/info" / f"{PACKAGE}.list").is_file():
        die(f"{PACKAGE} was not installed into the stage")
    stamp_write(stage, "debs", want)
    return True


# --------------------------------------------------------------------------
# stage: hooks
# --------------------------------------------------------------------------
def stage_hooks(args, R: Runner, prof: Profile, ch: Chroot,
                force: bool = False) -> bool:
    """Run the profile's hooks in the chroot; True when it ran.

    Skipped when the hooks hash matches and no earlier stage rebuilt the tree
    (`force`): a package or overlay rebuild changes what a hook sees.
    """
    stage = ch.stage
    log(f"-- hooks: {len(prof.hooks)} scripts, ascending --")
    if args.backend == "none":
        log("  [skip] backend none: hooks need a chroot")
        return False
    want = hooks_hash(prof)
    if not R.dry and not force and stamp_read(stage, "hooks") == want:
        log(f"  [skip] hooks: unchanged "
            f"({short(stamp_path(stage, 'hooks'))})")
        LAST["hooks"] = list(prof.hooks)
        return False
    done = []
    for name in prof.hooks:
        path = f"{PROFILE_MNT}/hooks/{name}"
        log(f"  {name}")
        ch.run(R, f'set -e\nsh "{path}"')
        done.append(name)
    if not R.dry:
        stamp_write(stage, "hooks", want)
    LAST["hooks"] = done
    return not R.dry



# --------------------------------------------------------------------------
# stage: modules
# --------------------------------------------------------------------------
def kernel_release(args, R: Runner, tree: Path, env: dict) -> str:
    out = R.run(["make", "-s", "-C", str(tree), "ARCH=arm64", "kernelrelease"],
                env=env, capture=True)
    return out.stdout.strip() if out else ""


def image_kernel_version(tree: Path) -> str:
    """The 'Linux version ...' string baked into the built Image, if any."""
    image = tree / "arch/arm64/boot/Image"
    if not image.exists():
        return ""
    out = subprocess.run(["strings", "-a", str(image)], capture_output=True,
                         text=True).stdout
    m = re.search(r"Linux version (\S+)", out)
    return m.group(1) if m else ""


def stage_modules(args, R: Runner, stage: Path) -> bool:
    """Ship the FIT kernel's modules in the image (host-side cross build).

    Runs on the host, never in the chroot: `make` here is the cross compiler,
    and `modules_install` only copies files plus `depmod -b <stage>`, which is
    architecture agnostic.  The release has to equal the FIT kernel's, or the
    board silently autoloads nothing.

    Skipped when the kernel config, release and built .ko hash match and the
    stage still carries the installed modules; a re-extracted base drops the
    stamp with the tree, so this runs again.
    """
    log("-- modules: FIT kernel modules -> stage --")
    if args.no_modules:
        log("  [skip] --no-modules")
        return False
    tree = args.kernel_tree.resolve()
    if R.dry:
        R.run(["make", "-s", "-C", str(tree), "ARCH=arm64", "kernelrelease"],
              env=build_env())
        R.run(["make", "-C", str(tree), f"-j{args.jobs}", "ARCH=arm64",
               "modules"], env=build_env())
        R.run(["make", "-C", str(tree), "ARCH=arm64",
               f"INSTALL_MOD_PATH={stage}", "modules_install"],
              env=build_env())
        return False
    if not (tree / ".config").exists():
        die(f"{tree}/.config is missing: no kernel to take modules from")
    env = build_env()             # t2-build.py's cross environment
    rel = kernel_release(args, R, tree, env)
    if not rel:
        die(f"could not read the kernel release of {tree}")
    baked = image_kernel_version(tree)
    if baked and baked != rel:
        die(f"{tree} is inconsistent: kernelrelease {rel} but the built Image "
            f"says {baked}; the FIT would boot a different kernel than the "
            "modules in the image")
    want = modules_stamp(tree, rel)
    dep = stage / "lib/modules" / rel / "modules.dep"
    if stamp_read(stage, "modules") == want and dep.is_file():
        log(f"  [skip] modules: unchanged "
            f"({short(stamp_path(stage, 'modules'))})")
        LAST["modules"] = module_facts_from_stage(args, stage)
        return False
    log(f"  kernel {rel} (tree {tree})")
    # Plain `make modules` leaves the .ko scattered across the tree; only
    # `modules_install INSTALL_MOD_PATH=...` creates lib/modules/<rel>/.
    # So the rebuild check has to look for them where they actually are.
    # Never skip this because .ko files exist: kbuild only knows a module is
    # current from its per-module config dependency files, and a .config edit
    # (t2-build.py --trim-config) must rebuild every module that depends on
    # what changed.  `make modules` is a fast no-op when the tree is current,
    # so the existence check can only ever ship stale modules.
    n_ko = sum(1 for _ in tree.rglob("*.ko") if _.is_file())
    log(f"  {n_ko} .ko in the tree; running `make modules` to bring them to "
        "the current .config")
    R.run(["make", "-C", str(tree), "ARCH=arm64", "olddefconfig"], env=env)
    # .config and include/config/auto.conf drift apart after an out-of-tree
    # edit (t2-build.py --trim-config), and kbuild then falls into
    # conf --oldconfig and blocks on a prompt.  Sync here, where olddefconfig
    # is non-interactive, then re-assert the curated lists: a build that
    # quietly lost a load-bearing symbol is worse than a build that stops.
    bad = T2B.config_problems()
    if bad:
        die(f"{tree}/.config no longer matches the curated lists: "
            + "; ".join(bad))
    R.run(["make", "-C", str(tree), f"-j{args.jobs}", "ARCH=arm64",
           "modules"], env=env)
    # Modules carry DWARF (CONFIG_DEBUG_INFO) that the Image never does: the
    # 1,591-module set is ~349 MB apparent of the 6 GiB rootfs, and stripping
    # takes it to ~60-90 MB [INFERENCE] before MODULE_COMPRESS_ZSTD cuts it
    # further.  Strip in the tree rather than the stage: modules_install
    # compresses on the way in, so stripping afterwards means unpacking the
    # .ko.zst again.  These are aarch64 objects, so the host `strip` cannot
    # read them - it fails with "Unable to recognise the format", so the
    # cross binutils are required.
    # The cross binutils are not on the host PATH: build_env() prepends
    # tools/cross/root/usr/bin, which is where aarch64-linux-gnu-strip lives.
    strip = shutil.which("aarch64-linux-gnu-strip", path=env["PATH"])
    if not strip:
        die("aarch64-linux-gnu-strip is missing: stripping module DWARF needs "
            "the cross binutils, not the host strip")
    R.run(["find", str(tree), "-name", "*.ko", "-exec", strip,
           "--strip-debug", "{}", "+"], env=env)
    # modules_install never deletes: stale .ko for this release would survive
    # (the previous 1,591-module set is ~349 MB apparent), so clear the target
    # release dir first.  Only this release's dir - the base rootfs's own
    # kernel modules live under a different <rel>.
    target = stage / "lib/modules" / rel
    if target.exists():
        R.run(["rm", "-rf", str(target)], env=env)
    R.run(["make", "-C", str(tree), "ARCH=arm64",
           f"INSTALL_MOD_PATH={stage}", "modules_install"], env=env)
    dep = stage / "lib/modules" / rel / "modules.dep"
    if not dep.is_file() or not dep.stat().st_size:
        die(f"{dep} is missing or empty after modules_install")
    git = subprocess.run(["git", "-C", str(tree), "describe", "--tags",
                          "--always", "--dirty"], capture_output=True,
                         text=True).stdout.strip()
    # MODULE_COMPRESS_ZSTD installs *.ko.zst, so count either spelling
    ko = sum(1 for p in (stage / "lib/modules" / rel).rglob("*")
             if p.is_file() and (p.name.endswith(".ko")
                                 or p.name.endswith(".ko.zst")))
    log(f"  installed {ko} modules for {rel} ({git})")
    # Computed *after* the strip above: the stage strips the .ko in the tree,
    # so a hash taken before it would never match the next run's pre-check.
    stamp_write(stage, "modules", modules_stamp(tree, rel))
    LAST["modules"] = {"tree": str(tree), "release": rel, "git": git,
                       "image_version_string": baked or None,
                       "config_sha256": sha256_file(tree / ".config"),
                       "modules": ko, "modules_dep_sha256": sha256_file(dep)}
    return True


def module_facts_from_stage(args, stage: Path) -> dict:
    """Recover the module facts when verify runs without the modules stage.

    --stages verify has to still prove the modules, so the release is taken
    from what the stage actually carries and cross-checked against the
    kernel tree's kernelrelease.
    """
    moddir = stage / "lib/modules"
    rels = sorted(p.name for p in moddir.iterdir() if p.is_dir()) \
        if moddir.is_dir() else []
    if not rels:
        return {}
    if len(rels) > 1:
        die(f"{moddir} holds several kernel releases {rels}; the image would "
            "ship modules for more than one kernel")
    rel = rels[0]
    tree = args.kernel_tree.resolve()
    want = kernel_release(args, Runner(True), tree, build_env()) \
        if (tree / "Makefile").exists() else ""
    if want and want != rel:
        die(f"stage ships modules for {rel} but {tree} is {want}: the FIT "
            "kernel and the image's modules must be the same build")
    dep = moddir / rel / "modules.dep"
    log(f"  modules in the stage: {rel} "
        f"({sum(1 for p in (moddir / rel).rglob('*') if p.is_file() and (p.name.endswith('.ko') or p.name.endswith('.ko.zst')))} .ko)")
    return {"tree": str(tree), "release": rel,
            "git": subprocess.run(["git", "-C", str(tree), "describe", "--tags",
                                   "--always", "--dirty"], capture_output=True,
                                  text=True).stdout.strip(),
            "modules": sum(1 for p in (moddir / rel).rglob("*")
                           if p.is_file() and (p.name.endswith(".ko")
                                               or p.name.endswith(".ko.zst"))),
            "modules_dep_sha256": sha256_file(dep) if dep.is_file() else None}

# --------------------------------------------------------------------------
# stage: image
# --------------------------------------------------------------------------
def stage_image(args, R: Runner, prof: Profile, stage: Path, out: Path,
                size: int) -> Path:
    log("-- image: stage -> ext4 (fakeroot, no root) --")
    img = out / prof.artifact
    blocks = size // 4096
    if R.dry:
        log(f"  [dry] would build {img} ({human(size)}, {blocks} blocks)")
        return img
    # the /t2-profile bind mount leaves empty placeholders behind; they are
    # not part of the image (proot never creates them, the sudo backend does)
    leftovers = stage / PROFILE_MNT.lstrip("/")
    if leftovers.exists():
        shutil.rmtree(leftovers)
    # The per-stage freshness stamps also live in the stage but are build
    # metadata, not image content: lift them out before mke2fs reads the tree
    # and put them back after, so the next run can still skip clean stages.
    stamps = stage / STAMP_DIR
    stashed = None
    if stamps.is_dir():
        stashed = {p.name: p.read_text() for p in stamps.iterdir()
                   if p.is_file()}
        shutil.rmtree(stamps)
    out.mkdir(parents=True, exist_ok=True)
    if img.exists():
        img.unlink()
    # truncate first, then mke2fs: unallocated blocks stay holes, and
    # lazy_itable/lazy_journal (the -d defaults, stated explicitly) keep the
    # inode table and journal out of the untouched tail
    R.run(["truncate", "-s", str(size), str(img)])
    inner = "\n".join([
        "set -e",
        f"chown -R 0:0 {shlex.quote(str(stage))}",
        f"mke2fs -q -t ext4 -F -L {shlex.quote(prof.label)} -b 4096 -m 1 "
        + (f"-U {shlex.quote(prof.fs_uuid)} " if prof.fs_uuid else "")
        + f"-E lazy_itable_init=1,lazy_journal_init=1 "
        f"-d {shlex.quote(str(stage))} {shlex.quote(str(img))} {blocks}",
    ])
    R.run(["fakeroot", "bash", "-c", inner])
    if stashed is not None:
        stamps.mkdir(parents=True, exist_ok=True)
        for name, text in stashed.items():
            (stamps / name).write_text(text)
    st = img.stat()
    apparent = st.st_size
    allocated = allocated_bytes(img)
    log(f"  {img}: {human(apparent)} apparent, {human(allocated)} allocated "
        f"({blocks} blocks of 4096)")
    if allocated >= apparent:
        die(f"{img} is not sparse: {human(allocated)} allocated of "
            f"{human(apparent)} apparent")
    LAST["image"] = {"path": str(img), "size": apparent, "blocks": blocks,
                     "label": prof.label, "fs": "ext4", "block_size": 4096,
                     "apparent_bytes": apparent, "allocated_bytes": allocated,
                     "mke2fs_extent": "lazy_itable_init=1,lazy_journal_init=1",
                     "fs_uuid": prof.fs_uuid or None}
    return img


def allocated_bytes(path: Path) -> int:
    """Blocks the file actually occupies (sparse-aware, no raw stat).

    Plain `du` is disk usage by default; the `--apparent-size=no` spelling is
    rejected outright by this host's coreutils, which silently sent the old
    code down the st_blocks fallback for every image.
    """
    r = subprocess.run(["du", "-B1", str(path)], capture_output=True,
                       text=True)
    out = r.stdout.split()
    if r.returncode == 0 and out and out[0].isdigit():
        return int(out[0])
    return path.stat().st_blocks * 512


# --------------------------------------------------------------------------
# stage: verify  (file-based; the board is never involved)
# --------------------------------------------------------------------------
class Checks:
    """Every check prints its raw evidence; a failure fails the build."""

    def __init__(self) -> None:
        self.rows: list = []

    def add(self, name: str, ok: bool, raw: str) -> bool:
        self.rows.append({"check": name, "ok": bool(ok), "raw": raw.strip()})
        log(f"  [{'ok' if ok else 'FAIL'}] {name}")
        return ok

    @property
    def failed(self) -> list:
        return [r["check"] for r in self.rows if not r["ok"]]


def debugfs(img: Path, query: str) -> str:
    return subprocess.run(["debugfs", "-R", query, str(img)],
                          capture_output=True, text=True).stdout


def verify_fsck(img: Path) -> dict:
    p = subprocess.run(["e2fsck", "-fn", str(img)], capture_output=True,
                       text=True)
    raw = (p.stdout + p.stderr).strip()
    m = re.search(r"([\d,]+)/([\d,]+) blocks", p.stdout)
    used = total = None
    if m:
        used, total = (int(x.replace(",", "")) for x in m.groups())
    return {"rc": p.returncode, "raw": raw, "used_blocks": used,
            "total_blocks": total}


def verify_machine_id(img: Path) -> tuple:
    """The image must ship an *empty* /etc/machine-id, never a baked one.

    machine-id(5) ("Safely Building Images") documents an empty file for images
    used on many machines: systemd(1) generates a transient ID for it at boot
    and systemd-machine-id-commit.service commits that to disk.  A non-empty
    file here means the build baked one shared ID - ubuntu-base's postinst does
    exactly that - and every board flashed from this image would share it; the
    first-boot identity unit would then find nothing to regenerate.  Returns
    (ok, raw, size-in-bytes-or-None).
    """
    raw = debugfs(img, "stat /etc/machine-id")
    m = re.search(r"Size:\s+(\d+)", raw)
    size = int(m.group(1)) if m else None
    return size == 0, raw, size


# `debugfs -R "ls -l <dir>"` columns: inode mode (nlink) uid gid size date
# time name - e.g. "  16  120777 (7)   0   0  31 30-Sep-2026 16:43 ssh.service"
LS_LINE = re.compile(r"^\s*\d+\s+\d+\s+\(\d+\)\s+(\d+)\s+(\d+)\s+")


def verify_root_ownership(img: Path) -> tuple:
    raw = debugfs(img, "ls -l /")
    bad, rows = [], 0
    for line in raw.splitlines():
        m = LS_LINE.match(line)
        if not m:
            continue
        rows += 1
        if m.groups() != ("0", "0"):
            bad.append(line.strip())
    return rows, bad, raw


def verify_fit(prof: Profile, fit_path: Path | None = None) -> dict | None:
    """Proof that the shipped FIT matches the sources it claims to come from.

    The FIT is a *separate* artifact from `rootfs.ext4`, and nothing else in
    this driver looks inside it.  A stale FIT is invisible to every file-level
    check here - measured 2026-09-30: all checks passed while the board booted
    the *bring-up* rootfs on p11, because the FIT's `chosen/bootargs` still said
    `root=PARTUUID=614e0000-0000`, which the bring-up initramfs cannot resolve,
    while the DTS said `root=LABEL=zspace-rootfs`.  So the FIT is read the way
    U-Boot would:

      * `/images/fdt` must be byte-identical to the profile's DTB,
      * `/images/kernel` must be byte-identical to that tree's `Image`,
      * `chosen/bootargs` must select the root by the profile's label.

    `fit_path` overrides the artifact path (used by
    `scripts/tests/test-fit-freshness.sh`).
    """
    spec = prof.fit
    if not spec:
        log('  [skip] profile declares no "fit" block (distro/README.md)')
        return None
    path = fit_path or (ROOT / spec["artifact"])
    tree = ROOT / spec.get("tree", str(DEFAULT_KERNEL_TREE.relative_to(ROOT)))
    kernel = tree / spec.get("kernel", "arch/arm64/boot/Image")
    dtb = tree / spec.get("dtb", "arch/arm64/boot/dts/rockchip/rk3568-t2.dtb")
    label = prof.label
    checks: list = []
    summary = {"artifact": str(path), "label": label}

    if not path.is_file():
        checks.append((f"FIT {path.name} exists", False,
                       f"{path} is missing - build it with scripts/rk-fit.py"))
        return {"checks": checks, "summary": summary}

    data = path.read_bytes()
    nodes = rkimg.fdt_parse(data, 0)
    summary["bytes"] = len(data)
    if nodes is None:
        checks.append((f"FIT {path.name} parses as a FIT", False,
                       "not an FDT/FIT structure"))
        return {"checks": checks, "summary": summary}

    def blob(name: str) -> bytes | None:
        props = nodes.get(f"/images/{name}")
        if not props:
            return None
        pos = rkimg.fdt_prop_int(props, "data-position")
        size = rkimg.fdt_prop_int(props, "data-size")
        if pos is None or not size or pos + size > len(data):
            return None
        return data[pos:pos + size]

    fdtblob, kernblob = blob("fdt"), blob("kernel")
    if fdtblob is None or kernblob is None:
        checks.append((f"FIT {path.name} carries fdt+kernel", False,
                       f"nodes: {sorted(nodes.get('/images', {}))}"))
        return {"checks": checks, "summary": summary}

    for name, src, got in (("fdt", dtb, fdtblob), ("kernel", kernel, kernblob)):
        label_txt = f"FIT {name} == {src.relative_to(ROOT)}"
        if not src.is_file():
            checks.append((label_txt, False,
                           f"{src} is missing - build the kernel tree "
                           f"(scripts/t2-build.py)"))
            continue
        want = src.read_bytes()
        same = got == want
        detail = (f"fit {name} {len(got):,} B sha256 "
                  f"{hashlib.sha256(got).hexdigest()[:16]}; {src.name} "
                  f"{len(want):,} B sha256 "
                  f"{hashlib.sha256(want).hexdigest()[:16]}")
        if not same:
            detail += " - the FIT is stale: rebuild it with scripts/rk-fit.py"
        checks.append((label_txt, same, detail))

    # bootargs live in the *embedded* DTB, not in the FIT structure itself:
    # parse /images/fdt as its own FDT and read its /chosen node.
    fdt_nodes = rkimg.fdt_parse(fdtblob, 0) or {}
    bootargs = rkimg.fdt_prop_str(fdt_nodes.get("/chosen", {}), "bootargs") or ""
    summary["bootargs"] = bootargs
    want = f"root=LABEL={label}"
    ok = want in bootargs.split() and "PARTUUID" not in bootargs
    detail = f"chosen/bootargs = {bootargs!r}"
    if not ok:
        detail += f" - expected a {want!r} token and no PARTUUID"
    checks.append((f"FIT bootargs resolve root by label ({label})", ok, detail))
    return {"checks": checks, "summary": summary}


UNIT_NAME = re.compile(r"^[A-Za-z0-9@._:\\-]+\.(service|socket|target|"
                        r"timer|path|mount|slice|device)$")


def verify_units(img: Path) -> tuple:
    """Enablement as symlinks under /etc/systemd/system/*.wants/.

    Returns every unit the image enables, not only the ones this profile
    happens to require: debugfs lists a symlink as the bare unit name, so the
    last token of each line is the name.
    """
    sysdir = debugfs(img, "ls -l /etc/systemd/system")
    wants = sorted(set(re.findall(r"(\S+\.wants)\s*$", sysdir, re.M)))
    found: dict = {}
    listings: dict = {}
    for d in wants:
        raw = debugfs(img, f"ls -l /etc/systemd/system/{d}")
        listings[d] = raw
        for line in raw.splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[-1]
            if UNIT_NAME.match(name):
                found.setdefault(name, []).append(d)
    return found, listings


def compressibility(img: Path) -> dict:
    """zstd -1 of the artifact, i.e. what a distribution would ship."""
    out = subprocess.run(f"zstd -1 -T0 -q -c {shlex.quote(str(img))} | wc -c",
                         shell=True, capture_output=True, text=True).stdout
    n = int(out.strip() or 0)
    size = img.stat().st_size
    return {"tool": "zstd -1 -T0", "compressed_bytes": n,
            "apparent_bytes": size,
            "ratio": round(n / size, 6) if size else None}

def stage_verify(args, prof: Profile, stage: Path, img: Path,
                 size: int) -> dict:
    log("-- verify: file-based proof of the image (no boot) --")
    c = Checks()

    fsck = verify_fsck(img)
    c.add(f"e2fsck -fn {img.name} (rc=0)", fsck["rc"] == 0, fsck["raw"])
    if fsck["used_blocks"] and fsck["total_blocks"]:
        fill = fsck["used_blocks"] * 100.0 / fsck["total_blocks"]
        log(f"  ext4 usage: {fsck['used_blocks']:,}/{fsck['total_blocks']:,} "
            f"blocks ({fill:.2f}% of {human(size)})")
        LAST["fill_percent"] = round(fill, 4)

    for path in REQUIRED_PATHS:
        raw = debugfs(img, f"stat {path}")
        c.add(f"image contains {path}", "Inode:" in raw, raw)

    # /sbin/init has to *resolve* to the systemd we shipped: a broken or
    # dangling link is an image that never boots, and its presence alone
    # (a symlink inode) would otherwise pass the check above.
    init = debugfs(img, "stat /sbin/init")
    target = re.search(r"Fast link dest:\s*\"?([^\"\n]+)", init)
    c.add("/sbin/init resolves to systemd", bool(target)
          and "systemd" in target.group(1), init)

    # DNS: /etc/resolv.conf has to be the systemd-resolved stub symlink.  An
    # empty regular file (what ubuntu-base ships) passes any "exists" check but
    # leaves the board with no nameserver at all - measured 2026-09-30.
    # The FIT is a separate artifact with its own stale-input failure mode:
    # a stale one boots the wrong partition while every check here passes.
    if getattr(args, "no_fit_check", False):
        log('  [skip] --no-fit-check: the FIT is built later by images/')
    else:
        fitres = verify_fit(prof)
        if fitres:
            for name, ok, raw in fitres["checks"]:
                c.add(name, ok, raw)
            LAST["fit"] = fitres["summary"]

    resolv = debugfs(img, "stat /etc/resolv.conf")
    rtarget = re.search(r"Fast link dest:\s*\"?([^\"\n]+)", resolv)
    c.add("/etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf",
          bool(rtarget) and rtarget.group(1).strip() ==
          "/run/systemd/resolve/stub-resolv.conf", resolv)

    # Identity: a baked machine-id is a fleet-wide secret shared by every
    # board flashed from this image (the reason t2-firstboot-identity.service
    # exists).  An empty file is what systemd(1) documents for multi-machine
    # images; PID1 generates a per-board ID and commits it on first boot.
    ok, raw, n = verify_machine_id(img)
    c.add("/etc/machine-id is empty (per-board ID generated on first boot)",
          ok, raw + ("" if ok else
                     f"\n{n} bytes baked into the image - every flashed "
                     f"board would share this machine-id"))
    LAST["machine_id_bytes"] = n

    # ssh is the one remote way in, so the authorized key must not be
    # group/world readable.
    # Presence is tested the way the REQUIRED_PATHS check above does it: a
    # present file makes debugfs print an "Inode:" line.  Do not test for the
    # absence message - debugfs writes "File not found by ext2_lookup" to
    # stderr, which this helper does not capture, so a missing file yields an
    # empty string, not a "not found" one.
    raw = debugfs(img, "stat /root/.ssh/authorized_keys")
    absent = "Inode:" not in raw
    c.add("no ssh key baked into the image", absent,
          raw if absent else
          f"\n{raw}\nthis key would be identical on every flashed board - "
          "provision keys from the config partition instead")
    if not absent:
        # debugfs prints "Inode: N Type: regular Mode:  0600 Flags: ..."
        mode = re.search(r"Mode:\s+0*(\d+)", raw)
        perm = int(mode.group(1), 8) & 0o7777 if mode else None
        c.add("/root/.ssh/authorized_keys is mode 600", perm == 0o600, raw)

    # Reachability: the image must have a documented way in on the consoles.
    # Root ships locked in the base tarball and the image carries no WiFi
    # credentials (P3's job) and no other user, so hooks/40-console.sh sets
    # a static root password (Raspbian-style) and both consoles are plain
    # password logins.  Autologin would be a silent passwordless root - easy
    # to miss - so its absence is asserted too.
    shadow = debugfs(img, "cat /etc/shadow")
    m = re.search(r"^root:([^:]*):", shadow, re.M)
    root_line = next((l for l in shadow.splitlines()
                      if l.startswith("root:")), "")
    hashed = bool(m) and len(m.group(1)) >= 20 and m.group(1)[:1] not in ("*", "!")
    c.add("root has a password hash (not locked) in /etc/shadow", hashed, root_line)
    tty1 = debugfs(img, "cat /etc/systemd/system/getty@tty1.service.d/autologin.conf")
    ttyS2 = debugfs(img, "cat /etc/systemd/system/serial-getty@ttyS2.service.d/baud.conf")
    c.add("no autologin on HDMI console (tty1)", "--autologin" not in tty1, tty1[:500])
    c.add("no autologin on serial console (ttyS2)", "--autologin" not in ttyS2, ttyS2)
    issue = debugfs(img, "cat /etc/issue")
    c.add("/etc/issue states the login", "root" in issue and "passwd" in issue, issue[:500])

    # udev's hardware database must be a real one.  A stub here means
    # systemd-hwe-hwdb's postinst died, which would leave udev resolving no
    # PCI/USB/ACPI names on the board.
    raw = debugfs(img, "stat /usr/lib/udev/hwdb.bin")
    # debugfs prints "User: 0 Group: 0 Project: 0 Size: N" on one line
    size = re.search(r"Size:\s+(\d+)", raw)
    n = int(size.group(1)) if size else 0
    magic = b""
    staged = stage / "usr/lib/udev/hwdb.bin"
    if staged.is_file():
        with open(staged, "rb") as f:
            magic = f.read(8)
    c.add("/usr/lib/udev/hwdb.bin is a real database", n >= 1 << 20,
          f"size in image: {n} bytes; magic in the staged file the image was "
          f"built from: {magic!r}")

    rows, bad, raw = verify_root_ownership(img)
    c.add(f"every one of the {rows} / entries is uid/gid 0", not bad and rows,
          raw + ("\nNOT root: " + "; ".join(bad) if bad else ""))

    found, listings = verify_units(img)
    raw = "\n".join(f"### /etc/systemd/system/{d}\n{l.strip()}"
                    for d, l in sorted(listings.items()))
    required = dedup(list(REQUIRED_UNITS) + prof.units)
    missing = [u for u in required if u not in found]
    c.add("systemd enablement symlinks: " + " ".join(required),
          not missing, raw + ("\nMISSING: " + " ".join(missing) if missing
                              else ""))
    log(f"       {len(found)} unit(s) enabled; {len(required)} required "
        f"({len(prof.units)} of them by this profile)")
    for unit in required:
        if unit in found:
            log(f"       {unit} -> {','.join(found[unit])}")

    raw = subprocess.run(["dumpe2fs", "-h", str(img)], capture_output=True,
                         text=True).stdout
    label = re.search(r"^Filesystem volume name:\s+(\S+)", raw, re.M)
    c.add(f"volume label is {prof.label}",
          bool(label) and label.group(1) == prof.label, raw)

    # modules: the image has to carry the FIT kernel's modules, or the board
    # boots with drivers it can never autoload
    mods = LAST.get("modules")
    if mods and not args.no_modules:
        rel = mods["release"]
        dep_path = f"/lib/modules/{rel}/modules.dep"
        raw = debugfs(img, f"cat {dep_path}")
        c.add(f"image contains {dep_path}", bool(raw.strip()), raw[:2000])
        c.add("modules.dep is not empty", len(raw.strip()) > 0,
              f"{len(raw)} bytes, {len(raw.splitlines())} lines")
        have = [m for m in REQUIRED_MODULES
                if re.search(rf"(^|/){re.escape(m)}\.ko(\.zst)?:", raw, re.M)]
        missing_mods = [m for m in REQUIRED_MODULES if m not in have]
        c.add("modules.dep provides " + " ".join(REQUIRED_MODULES),
              not missing_mods,
              "\n".join(line for line in raw.splitlines()
                        if any(re.search(rf"(^|/){re.escape(m)}\.ko(\.zst)?:",
                                        line)
                               for m in REQUIRED_MODULES))
              + ("\nMISSING: " + " ".join(missing_mods) if missing_mods
                 else ""))
        leaked = [p for p in FORBIDDEN_MODULES
                  if re.search(rf"^{re.escape(p)}(\.zst)?:", raw, re.M)]
        c.add("built-in drivers absent from modules.dep ("
              + " ".join(FORBIDDEN_MODULES) + ")", not leaked,
              "leaked: " + " ".join(leaked) if leaked
              else "no built-in driver has a module line")
        mods["verified_in_image"] = {"dep": dep_path, "bytes": len(raw),
                                     "found": have, "leaked": leaked}

    # a sparse image has to stay compressible: a 6 GiB file whose free space
    # is holes has to come out near the size of the used blocks
    ratio = compressibility(img)
    log(f"  zstd -1: {human(ratio['compressed_bytes'])} "
        f"({ratio['ratio']:.3f}x of the apparent size)")
    c.add("image compresses to about the used-block size",
          ratio["compressed_bytes"] < img.stat().st_size,
          f"apparent {human(img.stat().st_size)}, allocated "
          f"{human(allocated_bytes(img))}, zstd -1 "
          f"{human(ratio['compressed_bytes'])} (ratio {ratio['ratio']:.4f})")

    LAST["verification"] = {
        "checks": c.rows, "failed": c.failed,
        "units": dict(sorted(found.items())),
        "e2fsck_rc": fsck["rc"],
        "used_blocks": fsck["used_blocks"],
        "total_blocks": fsck["total_blocks"],
        "modules": LAST.get("modules"),
        "compressibility": ratio,
    }
    if c.failed:
        die("verification failed: " + ", ".join(c.failed))
    log(f"  all {len(c.rows)} checks passed")
    return LAST["verification"]


# --------------------------------------------------------------------------
# stage: manifest
# --------------------------------------------------------------------------
def write_manifest(out: Path, prof: Profile, args, tools: dict,
                   verification: dict) -> Path:
    img = out / prof.artifact
    # Re-measure here rather than trusting the image stage's number: mke2fs
    # returns with a lot of the file still dirty in the page cache, so a
    # measurement taken immediately under-reports the on-disk footprint by
    # ~12 MiB.  verify has just read the whole image, so the file is settled
    # by the time the manifest is written.
    art = artifact(img, prof.artifact, label=prof.label,
                   filesystem="ext4", block_size=4096,
                   fill_percent=LAST.get("fill_percent"))
    if LAST.get("image"):
        LAST["image"]["allocated_bytes"] = allocated_bytes(img)
    # A --stages run only knows what it ran.  Reading the manifest it is about
    # to replace lets a partial rerun keep the provenance of the full build
    # that produced the stage tree it is re-proving, instead of quietly
    # blanking hooks/base/probe and reporting the partial run's wall time as
    # if it were the whole build.
    prev = {}
    mpath = out / "manifest.json"
    if mpath.is_file():
        try:
            prev = json.loads(mpath.read_text())
        except ValueError:
            prev = {}

    def fact(key: str, default=None):
        """LAST's value for `key`, or the previous manifest's if this run
        did not produce one (None and [] mean "not run here")."""
        cur = LAST.get(key)
        return prev.get(key, default) if cur is None or cur == [] else cur

    manifest = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": str(Path(__file__).resolve()),
        "host": {"uname_m": os.uname().machine,
                 "python": sys.version.split()[0],
                 "cwd": str(ROOT)},
        "wall_time_s": LAST.get("wall_time_s"),
        "wall_time_note": LAST.get("wall_time_note"),
        "jobs": args.jobs,
        "profile": {
            "name": prof.name,
            "path": str(prof.dir),
            "extends": prof.extends,
            "layers": [str(d) for d in prof.layers],
            "artifact": prof.artifact,
            "label": prof.label,
            "size": prof.size,
            "partition": prof.image.get("partition"),
            "root": prof.root,
            # measured on the board, not by this build: p6 is unmounted
            # (the vendor root is p11), carries ~4.7 GiB of ZOS, so the
            # 6 GiB artifact plus the first-boot grow leaves p6 fully usable
            "live": {
                "partuuid": "614e0000-0000-4b53-8000-1d28000054a9",
                "size": 15032385536,
                "mounted": False,
                "vendor_used_blocks": 1222100,
                "source": "board measurement 2026-09-30 (blkid, dumpe2fs); "
                          "not measured by this build",
            },
        },
        "backend": {
            "name": args.backend,
            "probe": LAST.get("probe") or prev.get("backend", {}).get("probe"),
            "binds": LAST.get("binds", []),
            "env": CHROOT_ENV | prof.hook_env(),
        },
        "base": fact("base"),
        "tools": tools or prev.get("tools", {}),
        "packages": fact("packages"),
        "overlay": fact("overlay"),
        "debs": fact("debs"),
        "modules": LAST.get("modules") or prev.get("modules"),
        "hooks": fact("hooks", []),
        "image": LAST.get("image") or prev.get("image"),
        "fill_percent": LAST.get("fill_percent"),
        "verification": verification,
        "artifacts": [art],
        "flash": {},
    }
    part = prof.image.get("partition") or {}
    if part.get("lba") is not None:
        plan = flash_plan(img.stat().st_size, int(part["lba"]),
                          int(part.get("size") or prof.size))
        plan["writes"] = prof.artifact
        manifest["flash"][f"{part.get('name', 'rootfs')}_ext4"] = plan
    mpath = out / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")

    sums = out / "SHA256SUMS"
    sums.write_text(f"{art['sha256']}  {prof.artifact}\n")
    log(f"  manifest: {mpath}")
    log(f"  sums:     {sums}")
    return mpath


# --------------------------------------------------------------------------
# the /t2-profile bind: the profile plus image.json's firmware/ssh inputs
# --------------------------------------------------------------------------
def build_profile_binds(args, R: Runner, prof: Profile, payload: Path) -> list:
    """(host, guest) binds for the profile mount, or [] on a dry run."""
    binds = []
    image = prof.image
    if R.dry:
        binds.append((str(prof.dir), PROFILE_MNT))
        return binds
    shutil.rmtree(payload, ignore_errors=True)
    payload.mkdir(parents=True)
    src_dir = prof.dir
    if prof.layered:
        # The hooks of every layer have to run from one guest directory.  Copy
        # them into a merged view (parent first, so an overridden name ends up
        # as the child's file) and bind that; hooks only ever read the payload
        # subtrees below, so the view is all they need to see.
        view = payload / "profile"
        (view / "hooks").mkdir(parents=True)
        for d in prof.layers:
            for name in _hooks_at(d):
                shutil.copy2(d / "hooks" / name, view / "hooks" / name)
            # every other file in the profile directory stays visible at
            # /t2-profile/<name>, parent first and child last - the same thing
            # a single-layer profile gets by binding its own directory.
            for entry in sorted(d.iterdir()):
                if entry.name in ("hooks", "overlay") or not entry.is_file():
                    continue
                shutil.copy2(entry, view / entry.name)
        for name, data in (("base.json", prof.base), ("image.json", prof.image)):
            (view / name).write_text(json.dumps(data, indent=2) + "\n")
        (view / "packages.txt").write_text("\n".join(prof.packages) + "\n")
        src_dir = view
        log("  profile: " + " <- ".join(short(d) for d in prof.layers))
    binds.append((str(src_dir), PROFILE_MNT))
    src = image.get("firmware_src")
    if src:
        fw = (ROOT / src).resolve()
        if not fw.is_dir():
            die(f"image.json firmware_src {fw} does not exist")
        dst = payload / "firmware"
        # Stage the *contents* of firmware_src, so pointing it at a parent
        # directory (lib/firmware with brcm/ + rtl_nic/) keeps every hook path
        # unchanged: hook 50 reads /t2-profile/firmware/<subdir>/<blob>.
        shutil.copytree(fw, dst, dirs_exist_ok=True)
        binds.append((str(payload / "firmware"), f"{PROFILE_MNT}/firmware"))
        log(f"  firmware: {fw}/* -> {dst}")
    if image.get("ssh_key"):
        # Deliberately unsupported: a key baked here would be identical on every
        # flashed board (and in practice it was the developer's own key).  Keys
        # are provisioned per board from the config partition
        # (ssh.authorized_key=, applied by t2-provision.service) or installed by
        # the operator after logging in with the documented root password.
        die("image.json ssh_key is not supported: do not bake an ssh key into "
            "the image (use the config partition's ssh.authorized_key instead)")
    # A previous build may have left payload/ssh/authorized_keys behind (that is
    # how a stale copy of the developer's key kept coming back): the hook
    # installs /t2-profile/ssh/authorized_keys whenever it exists, so remove the
    # generated directory and its bind when the profile declares no key.
    shutil.rmtree(payload / "ssh", ignore_errors=True)
    return binds


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
# The environment the guest sees.  SYSTEMD_OFFLINE=1 is what makes
# `systemctl enable` operate on the files in the chroot instead of talking
# to a running systemd (distro/README.md).
CHROOT_ENV = {
    "PATH": CHROOT_PATH,
    "HOME": "/root",
    "TERM": "dumb",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
    "SYSTEMD_OFFLINE": "1",
}

# per-backend bind lists; the resolv.conf bind is what lets apt resolve the
# archive (the base tarball ships no /etc/resolv.conf)
BACKENDS = {
    "proot": [(str(RESOLV_CONF), "/etc/resolv.conf")],
    "sudo": [(str(RESOLV_CONF), "/etc/resolv.conf")],
    "none": [],
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, type=Path,
                    help="profile directory (rootfs/profiles/<name>)")
    ap.add_argument("--out", type=Path, default=ROOT / "build/rootfs",
                    help="output directory (default: build/rootfs)")
    ap.add_argument("--base-cache", type=Path,
                    default=ROOT / "build/rootfs-cache",
                    help="cached base tarballs (default: build/rootfs-cache)")
    ap.add_argument("--tools-cache", type=Path,
                    default=ROOT / "build/rootfs-tools",
                    help="fetched qemu/proot (default: build/rootfs-tools)")
    ap.add_argument("--rootfs-size", default=None,
                    help="image size in bytes (default: image.json size)")
    ap.add_argument("--kernel-tree", type=Path, default=DEFAULT_KERNEL_TREE,
                    help="kernel tree to take modules from (default: "
                         "build/kernel, written by kernel/fetch.sh)")
    ap.add_argument("--no-modules", action="store_true",
                    help="do not ship the FIT kernel's modules in the image")
    ap.add_argument("--no-fit-check", action="store_true",
                    help="skip the shipped-FIT freshness check; use when the "
                         "FIT (built by images/) does not exist yet, e.g. on a "
                         "rootfs-only build")
    ap.add_argument("--backend", choices=tuple(BACKENDS), default="proot",
                    help="chroot backend (default: proot, no root)")
    ap.add_argument("--no-keep-stage", action="store_true",
                    help="delete <out>/stage when the build finishes")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every command, run nothing")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="HTTP pipeline depth for apt inside the chroot")
    ap.add_argument("--stages", default=None,
                    help="comma separated subset of " + ",".join(STAGES) +
                         " to run (default: all); earlier stages must have "
                         "left their result in <out>/stage")
    args = ap.parse_args()

    R = Runner(args.dry_run)
    t0 = time.time()
    prof = Profile(args.profile)
    out = args.out.resolve()
    stage = out / "stage"
    payload = out / "profile-payload"
    size = parse_size(args.rootfs_size) if args.rootfs_size else prof.size
    if not size:
        die("no image size: image.json has none and --rootfs-size is unset")
    size -= size % 4096
    part = prof.image.get("partition") or {}
    part_size = int(part.get("size") or prof.size)
    if part.get("lba") is not None and size > part_size:
        die(f"image size {human(size)} does not fit the target partition "
            f"{human(part_size)}")

    log(f"t2-distro: profile {prof.name} -> {out}")
    log(f"  backend: {args.backend}   size: {human(size)}   jobs: {args.jobs}")

    # the /t2-profile mount (profile + image.json's firmware/ssh inputs) is
    # only read by the hooks, so only the hooks stage pays for it.


    binds = build_profile_binds(args, R, prof, payload)
    cached_proot, cached_qemu = cached_tools(args) \
        if args.backend != "none" else (None, None)
    ch = Chroot(args.backend, stage, BACKENDS[args.backend],
                CHROOT_ENV | prof.hook_env(),
                proot=cached_proot, qemu=cached_qemu)
    hook_ch = Chroot(args.backend, stage,
                     BACKENDS[args.backend] + binds,
                     CHROOT_ENV | prof.hook_env(),
                     proot=cached_proot, qemu=cached_qemu)
    LAST["binds"] = [f"{s} -> {d}" for s, d in ch.binds] + \
                    [f"{s} -> {d} (hooks only)" for s, d in binds]

    want = [s.strip() for s in args.stages.split(",")] if args.stages \
        else list(STAGES)
    unknown = [s for s in want if s not in STAGES]
    if unknown:
        die(f"unknown stage(s) {unknown}; choose from {list(STAGES)}")
    order = [s for s in STAGES if s in want]
    if "base" not in order and not stage.is_dir():
        die(f"{stage} is missing but the run needs {order}: run the earlier "
            "stages first (or drop --stages)")
    log(f"  stages : {' '.join(order)}")
    img = out / prof.artifact
    if "verify" in order and "modules" not in order and not args.no_modules:
        LAST["modules"] = module_facts_from_stage(args, stage)
        if not LAST["modules"]:
            die(f"{stage}/lib/modules is empty but the run needs it to verify: "
                "run the modules stage first (or pass --no-modules)")
    # The later stages read the image the image stage wrote; saying so beats
    # a FileNotFoundError traceback or a silently empty module check.
    if any(s in order for s in ("verify", "manifest")) \
            and "image" not in order and not img.is_file():
        die(f"{img} is missing but the run needs {order}: run the image stage "
            "first (or drop verify/manifest from --stages)")

    def on(name: str) -> bool:
        return name in order

    # Freshness: a stage whose stamped inputs still match is not re-run, and a
    # stage that does run makes the stages consuming its result run too
    # (packages -> overlay -> debs -> hooks).  A changed base tarball wipes the
    # stage tree, which drops every stamp, so a full rebuild follows.  tools
    # only fetches host binaries and never touches the tree; image/verify/
    # manifest always run against it.
    ran = False
    if on("base"):
        ran = stage_base(args, R, prof, stage) or ran
    if on("tools"):
        stage_tools(args, R, prof, stage, [ch, hook_ch])
    if on("packages"):
        ran = stage_packages(args, R, prof, ch) or ran
    if on("overlay"):
        ran = stage_overlay(args, R, prof, stage, force=ran) or ran
    if on("debs"):
        ran = stage_debs(args, R, prof, stage, ch, force=ran) or ran
    if on("hooks"):
        ran = stage_hooks(args, R, prof, hook_ch, force=ran) or ran
    if on("modules"):
        ran = stage_modules(args, R, stage) or ran
    if on("image"):
        img = stage_image(args, R, prof, stage, out, size)
    verification = ({} if args.dry_run or not on("verify")
                    else stage_verify(args, prof, stage, img, size))
    LAST["wall_time_s"] = round(time.time() - t0, 1)
    LAST["wall_time_note"] = "this run only; it is not the cost of a full \
build unless every stage ran"
    # Record what actually emulated the build even when the tools stage was
    # skipped: a --stages rerun writes a fresh manifest and must not blank the
    # provenance of the image it is describing.
    if args.backend != "none" and not LAST.get("tools") \
            and cached_proot and cached_qemu:
        LAST["tools"] = tool_facts(cached_proot, cached_qemu)
    if not on("manifest"):
        log("-- manifest: not in --stages, nothing written --")
    elif args.dry_run:
        log("-- manifest: [dry] would write manifest.json + SHA256SUMS --")
    else:
        log(f"-- manifest -- ({LAST['wall_time_s']}s)")
        write_manifest(out, prof, args, LAST.get("tools", {}), verification)

    if args.no_keep_stage and not args.dry_run:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(payload, ignore_errors=True)
        log(f"  removed {stage} and {payload} (--no-keep-stage)")

    log(f"t2-distro: done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
