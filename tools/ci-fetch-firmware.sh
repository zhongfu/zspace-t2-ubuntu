#!/usr/bin/env bash
#
# Fetch the vendor WiFi/BT firmware for a CI build.
#
# The AP6275P (BCM43752) blobs come from the vendor and are not redistributable,
# so they are not in the repository (rootfs/firmware/README.md).  CI provides
# them out of band:
#
#   T2_FIRMWARE_URL     tar or tar.gz holding the vendor files
#   T2_FIRMWARE_SHA256  optional sha256 of the downloaded archive
#
# The archive may hold the files at any depth; they are flattened into
# .ci/firmware/ and rootfs/fetch.sh --from-dir verifies every sha256 and
# installs rootfs/firmware/brcm/.
#
# Usage: T2_FIRMWARE_URL=https://... tools/ci-fetch-firmware.sh
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)

url=${T2_FIRMWARE_URL:-}
sha=${T2_FIRMWARE_SHA256:-}

if [ -z "$url" ]; then
    echo "ci-fetch-firmware: T2_FIRMWARE_URL is not set." >&2
    echo "  The vendor AP6275P firmware is not in this repository and CI cannot" >&2
    echo "  guess it.  Set the repository secret T2_FIRMWARE_URL to a tar(.gz)" >&2
    echo "  of the four vendor files, and T2_FIRMWARE_SHA256 to its digest." >&2
    echo "  See docs/releasing.md." >&2
    exit 1
fi

dir=$root/.ci/firmware
mkdir -p "$dir"

echo "ci-fetch-firmware: downloading the vendor firmware archive"
curl -fsSL "$url" -o "$dir.tar"
if [ -n "$sha" ]; then
    echo "$sha  $dir.tar" | sha256sum -c - >/dev/null
    echo "ci-fetch-firmware: archive sha256 verified"
fi
tar -C "$dir" -xf "$dir.tar"
rm -f "$dir.tar"

# rootfs/fetch.sh --from-dir wants the four files directly in the directory.
for f in fw_bcm43752a2_pcie_ag.bin clm_bcm43752a2_ag.blob \
         nvram_AP6275P.txt BCM4362A2.hcd; do
    if [ ! -f "$dir/$f" ]; then
        found=$(find "$dir" -type f -name "$f" -print -quit)
        if [ -z "$found" ]; then
            echo "ci-fetch-firmware: $f is not in the archive" >&2
            exit 1
        fi
        mv "$found" "$dir/$f"
    fi
done

# --from-dir verifies the sha256 of every file before installing it.
"$root/rootfs/fetch.sh" --from-dir "$dir"
