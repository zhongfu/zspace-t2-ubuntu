#!/bin/sh
# First-boot root filesystem growth.
#
# The artifact is built at 6 GiB while p6 is 14 GiB, so the image is small to
# flash and compresses; the remaining 8 GiB only become usable once the root
# filesystem has been grown to the end of its partition.
#
# `growpart` comes from cloud-guest-utils (packages.txt).  The script itself
# resolves the root partition from findmnt and only ever touches that one, so
# the vendor p7-p11 layout is left alone - unlike the vendor resize-helper,
# which resized every mounted filesystem.
set -e

# the overlay copy preserves modes; make it explicit anyway
chmod 755 /usr/local/sbin/t2-growroot.sh

systemctl enable t2-growroot.service

# the grow needs resize2fs (e2fsprogs) and a readable partition table
command -v growpart
command -v resize2fs
test -x /usr/local/sbin/t2-growroot.sh
