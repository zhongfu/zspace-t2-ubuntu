#!/usr/bin/env python3
"""Pin and fetch the component artefacts this image is built from.

The T2 build is split across four repositories: this one (initramfs, installer,
rootfs, image assembly) consumes artefacts built by zspace-t2-ubuntu-utils,
zspace-t2-kernel and zspace-t2-bootloader.  `components.lock` records, for each
component, the release tag and the sha256 of every artefact this repository was
built against, so a tag of this repository means one exact set of components and
a build can never silently pick up a different one.

The lock holds hashes, not URLs: where an artefact comes from is a property of
the environment, not of the build.  `fetch` resolves each name against, in
order, a local directory (--local / T2_COMPONENTS_DIR, for offline and
development builds) or the component's GitHub release (--url-base /
T2_COMPONENTS_URL_BASE).  Either way the bytes are hashed before they are used,
and a mismatch is fatal.

Usage:
  tools/components.py list   [--lock FILE]
  tools/components.py fetch  [--lock FILE] [--dest DIR] [--component NAME]
                             [--local DIR] [--url-base URL] [--check]
  tools/components.py lock   [--lock FILE] --dir DIR [--component NAME]
                             [--tag TAG] [--commit SHA] [--org ORG]

`fetch --check` verifies an already-populated directory without touching the
network, which is what CI runs before a release.  `lock` recomputes the lock
from a directory of real artefacts (a release download directory, or the
component repositories' own build output) and is how the lock is updated when a
component is bumped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LOCK = HERE.parent / "components.lock"
DEFAULT_DEST = HERE.parent / "build" / "components"
DEFAULT_ORG = "zspace"

# What each component publishes.  The patterns are the producers' contract
# (see the component repositories' release workflows); they are globs, never
# hard-coded file names, so a version bump does not touch this file.
ARTIFACTS = {
    "utils": ("t2-utils_*.deb",),
    "kernel": ("linux-image-*.deb", "linux-modules-*.deb",
               "linux-headers-*.deb", "Image", "rk3568-t2.dtb"),
    "bootloader": ("t2-bootloader_*.deb", "u-boot.itb", "idbloader.img",
                   "u-boot-installer.itb", "idbloader-installer.img",
                   "u-boot-initial-env", "u-boot-installer-initial-env"),
}

# The repository each component lives in.  Only the release URL needs this, and
# it is what `lock` writes into the lock so `fetch` can compose one.
REPOS = {
    "utils": "zspace-t2-ubuntu-utils",
    "kernel": "zspace-t2-kernel",
    "bootloader": "zspace-t2-bootloader",
}


def die(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_lock(path: Path) -> dict:
    if not path.is_file():
        die(f"no lock file at {path} (run `components.py lock --dir <dir>` first)")
    with path.open() as fh:
        try:
            lock = json.load(fh)
        except json.JSONDecodeError as exc:
            die(f"{path} is not valid JSON: {exc}")
    lock.setdefault("components", {})
    return lock


def rows(lock: dict, only: str | None):
    """Yield (component, spec, name, sha256) for every artefact in the lock."""
    for comp, spec in lock["components"].items():
        if only and comp != only:
            continue
        for name, sha in sorted(spec.get("artifacts", {}).items()):
            yield comp, spec, name, sha


def cmd_list(args) -> int:
    lock = read_lock(args.lock)
    total = 0
    for comp, spec, name, sha in rows(lock, args.component):
        print(f"{comp}\t{spec.get('tag', '?')}\t{spec.get('commit', '?')[:12]}\t"
              f"{name}\t{sha[:16]}")
        total += 1
    print(f"{total} artefact(s) pinned in {args.lock}")
    return 0


def source_for(args, spec: dict, comp: str, name: str) -> str:
    """Where an artefact comes from: local directory first, then the release.

    A local directory is searched as <dir>/<component>/<name> and then
    <dir>/<name>, so it can mirror the destination layout (which is also what a
    release download directory looks like once the components are unpacked).
    """
    if args.local:
        base = Path(args.local).expanduser()
        for candidate in (base / comp / name, base / name):
            if candidate.is_file():
                return str(candidate)
        return str(base / comp / name)
    org = args.org or os.environ.get("T2_COMPONENTS_ORG") or DEFAULT_ORG
    base = args.url_base or os.environ.get("T2_COMPONENTS_URL_BASE") \
        or f"https://github.com/{org}"
    repo = spec.get("repo") or die(f"lock entry for {name} has no repo")
    tag = spec.get("tag") or die(f"lock entry for {name} has no tag")
    return f"{base.rstrip('/')}/{repo}/releases/download/{tag}/{name}"


def obtain(src: str, dest: Path) -> None:
    """Copy or download src to dest (atomically), or die."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if src.startswith(("http://", "https://", "file://")):
            try:
                with urllib.request.urlopen(src) as resp, tmp.open("wb") as out:
                    shutil.copyfileobj(resp, out)
            except urllib.error.URLError as exc:
                die(f"cannot fetch {src}: {exc}")
        else:
            path = Path(src)
            if not path.is_file():
                die(f"no artefact at {path} and no network source configured")
            shutil.copyfile(path, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def cmd_fetch(args) -> int:
    lock = read_lock(args.lock)
    dest_root = Path(args.dest).expanduser()
    fetched = cached = 0
    for comp, spec, name, want in rows(lock, args.component):
        dest = dest_root / comp / name
        if dest.is_file() and sha256_file(dest) == want:
            print(f"  [cached] {comp}/{name}")
            cached += 1
            continue
        if args.check:
            die(f"{dest} is missing or does not match the lock "
                f"(want sha256 {want})")
        if dest.is_file():
            print(f"  [stale]  {comp}/{name} (sha256 differs, refetching)")
        src = source_for(args, spec, comp, name)
        print(f"  [fetch]  {comp}/{name} <- {src}")
        obtain(src, dest)
        got = sha256_file(dest)
        if got != want:
            dest.unlink(missing_ok=True)
            die(f"{name}: sha256 mismatch\n  expected {want}\n  got      {got}")
        fetched += 1
    print(f"components: {fetched} fetched, {cached} cached, into {dest_root}")
    return 0


def cmd_lock(args) -> int:
    """(Re)write the lock from a directory of real artefacts."""
    src = Path(args.dir).expanduser()
    if not src.is_dir():
        die(f"{src} is not a directory")
    lock = read_lock(args.lock) if args.lock.is_file() else {"components": {}}
    if args.org:
        lock["org"] = args.org
    lock.setdefault("org", DEFAULT_ORG)
    comps = lock.setdefault("components", {})

    for comp, patterns in ARTIFACTS.items():
        if args.component and comp != args.component:
            continue
        found = {}
        for pattern in patterns:
            for path in sorted(src.glob(pattern)):
                if path.is_file():
                    found[path.name] = sha256_file(path)
        if not found:
            die(f"{src} holds none of {comp}'s artefacts {patterns}")
        spec = comps.setdefault(comp, {})
        spec["artifacts"] = found
        spec["repo"] = args.repo or spec.get("repo") or REPOS[comp]
        if args.tag:
            spec["tag"] = args.tag
        if args.commit:
            spec["commit"] = args.commit
        spec.setdefault("tag", "unreleased")
        spec.setdefault("commit", "")
        print(f"{comp}: {len(found)} artefact(s), tag {spec['tag']}")
        for name in sorted(found):
            print(f"  {name}  {found[name][:16]}")

    lock["_comment"] = ("Component artefacts this image builds against. "
                        "Regenerate with tools/components.py lock; fetch and "
                        "verify with tools/components.py fetch.")
    with args.lock.open("w") as fh:
        json.dump(lock, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {args.lock}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
        p.add_argument("--component", help="restrict to one component")

    p = sub.add_parser("list", help="show the pinned artefacts")
    common(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("fetch", help="fetch and verify the pinned artefacts")
    common(p)
    p.add_argument("--dest", default=str(DEFAULT_DEST))
    p.add_argument("--local", help="directory to take the artefacts from, "
                                   "searched as <dir>/<component>/<name> then "
                                   "<dir>/<name>")
    p.add_argument("--url-base", help="prefix that <repo>/releases/download/"
                                      "<tag>/<name> is appended to "
                                      "(default: https://github.com/<org>)")
    p.add_argument("--org", help="GitHub org owning the component repos")
    p.add_argument("--check", action="store_true",
                   help="verify only; never fetch")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("lock", help="recompute the lock from a directory")
    common(p)
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--tag")
    p.add_argument("--commit")
    p.add_argument("--org")
    p.add_argument("--repo", help="override the component's repository name")
    p.set_defaults(func=cmd_lock)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
