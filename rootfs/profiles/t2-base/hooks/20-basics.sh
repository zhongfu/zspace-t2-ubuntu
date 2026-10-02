#!/bin/sh
# Identity, time base and the root filesystem entry.
set -e

echo t2 > /etc/hostname
sed -i '/^127\.0\.1\.1/d' /etc/hosts 2>/dev/null || true
printf '127.0.1.1\tt2\n' >> /etc/hosts

# Identity must NOT be baked: an image flashed onto many boards must not carry
# a machine-id that two boards would share.  machine-id(5) documents an *empty*
# /etc/machine-id for such images - PID1 generates a transient ID and
# systemd-machine-id-commit.service (statically enabled by the systemd package
# under /usr/lib/systemd/system/sysinit.target.wants/) commits it on first
# boot.  ubuntu-base's postinst creates BOTH /etc/machine-id and a *regular*
# /var/lib/dbus/machine-id with the same value, and systemd falls back to the
# D-Bus file when /etc/machine-id is empty (machine-id(5), "When a machine is
# booted") - so truncating only /etc/machine-id would still hand every board
# the same ID.  Point dbus at the canonical file so both read empty and systemd
# generates a fresh, per-board ID.  0 bytes is asserted by the distro driver's
# verify stage.
: > /etc/machine-id
chmod 644 /etc/machine-id
mkdir -p /var/lib/dbus
ln -snf /etc/machine-id /var/lib/dbus/machine-id

ln -snf /usr/share/zoneinfo/Etc/UTC /etc/localtime
printf 'Etc/UTC\n' > /etc/timezone

# The root device is deliberately NOT named in fstab.  The initramfs mounts /
# from the kernel cmdline (`root=LABEL=zspace-rootfs`) and passes `rootflags=`
# on to that mount, and the installer repartitions the eMMC - minting new
# PARTUUIDs - before writing this rootfs, so any identifier baked here would
# name a device that no longer exists on the next boot (systemd-fstab-generator
# really does emit a `-.mount` with that What=, measured).  What the old `/`
# line carried now arrives on the cmdline: the label (which the builder always
# writes, and which differs per medium: zspace-rootfs on the eMMC,
# zspace-cardroot on a card rootfs) and `rootflags=errors=remount-ro`.  An
# fstab without a root entry is legal - mounting / is the initramfs' job.
cat > /etc/fstab <<EOF
# <file system>                    <mount point>  <type>  <options>  <dump>  <pass>
# root                            /              -       from the kernel cmdline
#                                 (root=LABEL=... rootflags=errors=remount-ro)
EOF

# Networking is NetworkManager's job; make sure nothing else claims the
# interfaces first.
rm -f /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null || true
