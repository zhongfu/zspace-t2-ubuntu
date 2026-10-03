# Releasing

A release carries the installer SD-card image, the individual build
components, and the `t2-utils` package.  Two GitHub Actions workflows do the
work:

* `.github/workflows/release.yml` runs on a `v*` tag.  It builds every
  artefact, then publishes a GitHub release.
* `.github/workflows/build.yml` runs on pull requests and branch pushes.  It
  builds a fast subset (`build-all.sh` steps 2 to 6: initramfs, kernel and
  U-Boot), keeps no artefacts, and skips the slow rootfs and installer steps.
  When `T2_FIRMWARE_URL` is not set, it builds U-Boot only and says so.

Both workflows build in the Docker image from `Dockerfile`, through
`docker-build.sh`.  `docs/building.md` explains that image and the build steps.

## Set up a repository

Add two secrets in **Settings > Secrets and variables > Actions**:

| Secret | Contents |
|---|---|
| `T2_FIRMWARE_URL` | URL of a tar or tar.gz archive that holds the vendor firmware files |
| `T2_FIRMWARE_SHA256` | SHA-256 of that archive (recommended, catches a wrong or changed archive) |

The AP6275P WiFi and Bluetooth firmware comes from the vendor.  It is not
redistributable, so it is not in this repository (`rootfs/firmware/README.md`
lists it).  The archive must hold these four files, at any depth:

```
fw_bcm43752a2_pcie_ag.bin
clm_bcm43752a2_ag.blob
nvram_AP6275P.txt
BCM4362A2.hcd
```

The URL can be private and can carry a token.  The build does not republish the
archive.  It does embed the firmware in the images, and the release notes say so.
The workflow stops with a clear message when `T2_FIRMWARE_URL` is not set.

Set **Settings > Actions > General > Workflow permissions** to **Read and write
permissions**.  The release job needs write access to create the release.

The full build wants about 40 GB of free disk and a working Docker daemon.  The
workflows free disk space on the runner first.  A larger runner may still be
needed.

## Make a release

1. Confirm the branch is green in the **Build** workflow.
2. Pick the version, for example `v26.04.1`.
3. Create an annotated tag and push it:

```sh
git tag -a v26.04.1 -m "ZSpace T2 26.04.1"
git push origin v26.04.1
```

The tag starts the **Release** workflow.  It:

1. checks out the tag;
2. downloads the vendor firmware archive and installs it;
3. builds every artefact in the Docker image (`./docker-build.sh`);
4. collects the release files and writes `SHA256SUMS`;
5. stores them as a workflow artefact;
6. builds the release notes and creates the GitHub release.

The release notes have three parts: the changelog, a short explanation of every
file, and a note on the firmware licence.

## The release files

`tools/collect-release-artifacts.sh` produces this list.  It fails when a
required file is missing, so a broken build cannot publish an incomplete
release.

| File | What it is |
|---|---|
| `installer.img` | SD-card installer image.  Write it to a card and boot the board; it installs the system to the eMMC. |
| `rootfs.ext4.zst` | Ubuntu root filesystem, zstd-compressed.  The installer writes it to the eMMC. |
| `t2-utils_<version>_all.deb` | The `t2-utils` package: services, scripts and settings for the board. |
| `t2-utils-apt-repo.tar.gz` | The local apt repository shipped in the image: the package and its `Packages` index. |
| `Image` | The Linux kernel image. |
| `rk3568-t2.dtb` | The device tree blob for the T2 board. |
| `t2-mainline-boot.img` | The kernel FIT: kernel, device tree and initramfs in one bootable image. |
| `u-boot.itb` | U-Boot bootloader for the eMMC. |
| `idbloader.img` | Rockchip loader for the eMMC: DDR init and the U-Boot SPL. |
| `u-boot-installer.itb` | U-Boot bootloader for the SD-card installer. |
| `idbloader-installer.img` | Rockchip loader for the SD-card installer. |
| `u-boot-initial-env` | U-Boot default environment for the eMMC image. |
| `u-boot-installer-initial-env` | U-Boot default environment for the SD-card installer. |
| `Image.old` | Previous kernel image, for the U-Boot A/B fallback.  Only when the build provides it. |
| `SHA256SUMS` | SHA-256 checksums.  Check them with `sha256sum -c SHA256SUMS`. |

Most users only need `installer.img`.

The version in the `.deb` name comes from the profile base release
(`rootfs/profiles/t2-base/base.json`, `release`), not from the git tag.  The two
usually differ: the package version tracks the Ubuntu release it was built
against.

## Re-run a failed build

Open the failed run in **Actions > Release** and use **Re-run failed jobs**.
GitHub runs only the jobs that failed.  If the build job succeeded, the release
job downloads the stored artefacts and publishes them again; nothing rebuilds.

To rebuild everything without a new tag, use **Run workflow** on the **Release**
workflow and type the tag.

## Publish by hand

Use the same commands as the workflow.  Run them from the repository root.

1. Get the vendor firmware.  Use an existing `rootfs/firmware/`, or fetch it:

```sh
T2_FIRMWARE_URL=https://... T2_FIRMWARE_SHA256=... tools/ci-fetch-firmware.sh
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
git log --no-merges --pretty='* %s (%h)' v26.04.0..v26.04.1 > dist/changelog.md
cat dist/changelog.md dist/release-files.md > dist/release-notes.md
```

5. Create the release (requires the `gh` GitHub CLI):

```sh
gh release create v26.04.1 dist/release/* \
  --title v26.04.1 --notes-file dist/release-notes.md --verify-tag
```
