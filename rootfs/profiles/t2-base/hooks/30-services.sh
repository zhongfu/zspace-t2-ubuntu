#!/bin/sh
# Enable the services the profile needs.  systemctl enable works file-based in
# a chroot (the driver exports SYSTEMD_OFFLINE=1); acceptance is checked by the
# symlinks under /etc/systemd/system/*.wants/, not by starting anything.
set -e

systemctl enable NetworkManager.service
systemctl enable ssh.service
systemctl enable systemd-timesyncd.service
systemctl enable avahi-daemon.service
# systemd-resolved is what NetworkManager hands its DHCP-provided nameservers
# to (Ubuntu's default `dns=systemd-resolved`), so DNSSEC/stub lookups need it
# enabled - it arrives enabled from ubuntu-base, but say so explicitly rather
# than depend on the base tarball's preset.
systemctl enable systemd-resolved.service
systemctl enable t2-leds.service
systemctl enable t2-hddled.service
# Power button policy (item: logind must not power off on a short press).
# t2-powerkey.service measures the press: a short press is a no-op, a press
# held >= press_seconds (/etc/t2/powerkey.conf) asks for a graceful poweroff,
# and a very long hold is cut by the PMIC in hardware.
systemctl enable t2-powerkey.service
chmod 755 /usr/local/sbin/t2-powerkey.py
# Out-of-band provisioning: reads the `t2-config` FAT partition on boot (a
# no-op when the partition is absent, which is the eMMC layout today), then
# puts a USB gadget in front of it (network + the partition as a drive).
systemctl enable t2-provision.service
# Per-board first-boot identity: machine-id (fallback) + root fs UUID +
# derived default hostname.  It is ordered before t2-provision.service, so a
# `hostname=` key from the card still overrides the derived default.
systemctl enable t2-firstboot-identity.service
chmod 755 /usr/local/sbin/t2-firstboot-identity.sh
# Kernel A/B bookkeeping: clears `upgrade_available`/`bootcount` in the U-Boot
# environment (on the boot FAT) once a boot has proved good, and promotes
# `/Image.old` to `/Image` after a fallback boot.  Without it U-Boot's
# bootcounter keeps climbing and every 4th boot runs `altbootcmd` forever.
systemctl enable t2-boot-commit.service
chmod 755 /usr/local/sbin/t2-boot-commit.sh
systemctl enable t2-usbgadget.service
# DHCP for the gadget link only (see the /etc/dnsmasq.d drop-in: usb0).
systemctl enable dnsmasq.service
# BLE management: bluetoothd owns the controller (the firmware is in the image),
# t2-ble.service is the GATT application.  A missing controller is not fatal -
# the app exits and systemd retries.
systemctl enable bluetooth.service
systemctl enable t2-ble.service
chmod 755 /usr/local/sbin/t2-ble.py /usr/local/sbin/t2-ble-password

# ---- units this board never needs: mask, do not enable --------------------
# A mask is a symlink to /dev/null under /etc/systemd/system; an *empty file*
# there masks too, but then a later `systemctl enable` cannot replace it
# cleanly, so always use the symlink form and never leave an empty unit file.
#
# NetworkManager-wait-online.service: boot must NOT gate on internet
# reachability.  This is a portable NAS that may boot with no network at all -
# the user sets it up afterwards over BLE or the USB ethernet gadget - so
# dnsmasq on the gadget link must not be made to wait for network-online.target
# either.  Measured 2026-10-01: it sits on the critical chain multi-user <-
# dnsmasq <- network-online.target <- NetworkManager-wait-online (2.335 s of
# NetworkManager plus its wait).
ln -sf /dev/null /etc/systemd/system/NetworkManager-wait-online.service
# ext4 online scrub: e2scrub_reap.service (3.063 s in systemd-analyze blame,
# 2026-10-01) and e2scrub_all.timer periodically scrub *mounted* ext4
# filesystems - a server/desktop habit that never helps a single-rootfs
# appliance at boot.  e2fsprogs stays installed: resize2fs/e2fsck are
# load-bearing for t2-growroot and recovery.
ln -sf /dev/null /etc/systemd/system/e2scrub_reap.service
ln -sf /dev/null /etc/systemd/system/e2scrub_all.timer
# NVMe-oF autoconnect: nvmf-autoconnect.service (1.720 s) probes for NVMe
# fabrics (nvme discover/connect over TCP/RDMA/FC), and this board has no
# fabric target or initiator on a network it is configured for.  nvme-cli
# stays installed as the admin tool.
ln -sf /dev/null /etc/systemd/system/nvmf-autoconnect.service
# netplan-configure.service: NetworkManager here is configured directly - the
# /etc/NetworkManager/conf.d drop-ins plus the system-connections keyfiles that
# t2-provision.sh writes - so the netplan backend only costs 1.098 s at boot and
# configures nothing this image reads.  netplan.io itself must STAY installed:
# in this base network-manager hard-Depends on it (purging netplan.io resolves
# by removing network-manager and its dependents, measured 2026-10-01), so the
# unit is masked instead of the package removed.
ln -sf /dev/null /etc/systemd/system/netplan-configure.service

# /etc/resolv.conf is *not* fixed here: the driver binds the host's file into
# this chroot (so apt can resolve), and proot correctly refuses to unlink a bind
# source ("rm: cannot remove '/etc/resolv.conf': Permission denied", measured
# 2026-09-30).  The stub symlink is shipped by overlay/etc/resolv.conf instead.

# BT is rfkill-able and its controller is a UART device; nothing to enable.
