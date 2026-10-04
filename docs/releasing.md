# Releasing

A release carries what this repository builds: the installer SD-card image, the
rootfs image, the `t2-initramfs` package and `components.lock` (the exact
kernel, U-Boot and `t2-utils` it was built from).  Two GitHub Actions workflows
do the work:

* `.github/workflows/release.yml` runs on a date tag (`YYYYMMDD`).  It fetches
  the pinned components, builds every artefact, then publishes a GitHub release.
* `.github/workflows/build.yml` runs on pull requests and branch pushes.  One
  job fetches the pinned components and verifies them against `components.lock`;
  another builds the boot chain (`build-all.sh` steps 2-3: the vendor firmware
  and the initramfs) on `ubuntu-26.04-arm`, the architecture of the board, and
  keeps no artefacts.  The rootfs and the installer image (steps 4-5) are the
  slowest and need the most network, and release.yml builds them anyway.

Both workflows build in the Docker image from `Dockerfile`, through
`docker-build.sh`.  `docs/building.md` explains that image and the build steps.
The build works on x86-64 and arm64 hosts; on arm64 the rootfs chroot runs
natively instead of under QEMU.  The release builds on `ubuntu-latest` (x86-64)
by default: set the repository variable **T2_RELEASE_RUNNER** to
`ubuntu-26.04-arm` (Settings > Secrets and variables > Actions > Variables) to
build it on arm64 instead.

## Set up a repository

Two repository **variables** matter (Settings > Secrets and variables > Actions
> Variables):

* **T2_COMPONENTS_ORG** — optional.  `components.lock` records the GitHub org
  that owns `zspace-t2-kernel`, `zspace-t2-bootloader` and
  `zspace-t2-ubuntu-utils`, and the fetch uses it, so nothing has to be set.
  Set this only to pull the component artefacts from a fork or a mirror.
* **T2_RELEASE_RUNNER** — optional; the runner label for the release build
  (default `ubuntu-latest`).

The build embeds the vendor AP6275P WiFi and Bluetooth firmware, which is
committed in `rootfs/firmware/brcm/` (`rootfs/firmware/README.md` lists it and
explains why).  No secret is needed.

Set **Settings > Actions > General > Workflow permissions** to **Read and write
permissions**.  The release job needs write access to create the release.

The full build wants about 40 GB of free disk and a working Docker daemon.  The
workflows free disk space on the runner first.  A larger runner may still be
needed.

## Make a release

1. Confirm the branch is green in the **Build** workflow.
2. Tag the release with its date, `YYYYMMDD` (`20261004`).  A second release the
   same day adds a counter: `20261004.1`.  The workflow matches tags that start
   with eight digits - GitHub's tag filters are globs, not regular expressions.
3. Create an annotated tag and push it:

```sh
git tag -a 20261004 -m "ZSpace T2 20261004"
git push origin 20261004
```

The tag starts the **Release** workflow.  It:

1. checks out the tag;
2. resolves the tag and frees disk;
3. fetches the pinned component artefacts (`T2_COMPONENTS_ORG`), sha256-verified
   against `components.lock`;
4. builds every artefact in the Docker image (`./docker-build.sh`);
5. collects the release files and writes `SHA256SUMS`;
6. stores them as a workflow artefact;
7. builds the release notes and creates the GitHub release.

The release notes have three parts: the changelog, a short explanation of every
file followed by the component versions from `components.lock`, and a note on
the firmware licence.

## The release files

`tools/collect-release-artifacts.sh` produces this list.  It fails when a
required file is missing, so a broken build cannot publish an incomplete
release.

| File | What it is |
|---|---|
| `installer.img` | SD-card installer image.  Write it to a card and boot the board; it installs the system to the eMMC. |
| `rootfs.ext4.zst` | Ubuntu root filesystem, zstd-compressed.  The installer writes it to the eMMC. |
| `t2-initramfs_<version>_all.deb` | The installer initramfs: the card FIT's ramdisk, and the fallback `linux-image-<rel>-t2` uses for an on-board kernel upgrade when a rootfs has no initramfs-tools. |
| `components.lock` | The component releases and artefact sha256s this image was built from. |
| `SHA256SUMS` | SHA-256 checksums.  Check them with `sha256sum -c SHA256SUMS`. |

Most users only need `installer.img`.

The kernel, U-Boot, `t2-utils` and the raw boot-chain files are built and
published by the three component repositories; this release does not carry
them.

The `t2-initramfs` version comes from the profile base release
(`rootfs/profiles/t2-base/base.json`, `release`), not from the git tag.

## Re-run a failed build

Open the failed run in **Actions > Release** and use **Re-run failed jobs**.
GitHub runs only the jobs that failed.  If the build job succeeded, the release
job downloads the stored artefacts and publishes them again; nothing rebuilds.

To rebuild everything without a new tag, use **Run workflow** on the **Release**
workflow and type the tag.

## Publish by hand

Use the same commands as the workflow.  Run them from the repository root.

1. Fetch the pinned component artefacts from their releases (add
   `T2_COMPONENTS_ORG=<org>` to take them from a fork or a mirror instead):

```sh
tools/components.py fetch
```

2. Build everything:

```sh
./docker-build.sh
```

3. Collect the files and write the file list:

```sh
./docker-build.sh tools/collect-release-artifacts.sh --notes dist/release-files.md
```

4. Make the notes from the changelog and the file list:

```sh
git log --no-merges --pretty='* %s (%h)' 20261001..20261004 > dist/changelog.md
cat dist/changelog.md dist/release-files.md > dist/release-notes.md
```

5. Create the release (requires the `gh` GitHub CLI):

```sh
gh release create 20261004 dist/release/* \
  --title 20261004 --notes-file dist/release-notes.md --verify-tag
```
