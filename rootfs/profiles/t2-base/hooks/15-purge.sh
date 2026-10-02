#!/bin/sh
# Purge daemons the T2 has no hardware or configuration path for.
#
# apt-get installs with Recommends on (the driver passes
# APT::Install-Recommends=true), which is how modemmanager got in even though
# it is not named in packages.txt.  It costs measured boot time: 1.771 s in
# systemd-analyze blame (2026-10-01) on a board with no LTE/CDMA modem of any
# kind, because network-manager Recommends it.
# --auto-remove is what takes its private dependencies (libqmi/libmbim,
# usb-modeswitch) with it; the loop below then proves it did not take anything
# load-bearing.
#
# netplan.io is deliberately NOT purged here, although it also arrived as a
# Recommends and its netplan-configure.service cost 1.098 s at boot: in this
# base (Ubuntu 26.04 "resolute") network-manager *Depends* on netplan.io
# (`network-manager 1.54.3-2ubuntu3.1` Depends: ... netplan.io (>= 1.2~)),
# so `apt-get purge netplan.io` resolves by removing network-manager too -
# measured 2026-10-01, the purge took network-manager, network-manager-l10n,
# network-manager-pptp, libmm-glib0, libndp0 and ppp with it.  Purging it is
# unsafe for this profile as long as NetworkManager is the network manager;
# the check below is what catches that if the dependency ever widens.
set -e
export DEBIAN_FRONTEND=noninteractive

purge_if_installed() { # package
    status=$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)
    if [ "$status" = "install ok installed" ]; then
        echo "t2-purge: removing $1"
        apt-get purge -y --auto-remove "$1"
    else
        # A package that is not installed (a future base tarball may drop the
        # Recommends) must not fail the build.
        echo "t2-purge: $1 not installed - skipping"
    fi
}

purge_if_installed modemmanager

# --auto-remove must not have carried off anything the image needs: the boot,
# the grow, DHCP on the gadget link, WiFi and the admin nvme tool all have to
# still be configured afterwards, or the purge has broken a package set that
# packages.txt deliberately chose.
for pkg in network-manager dnsmasq wpasupplicant e2fsprogs cloud-guest-utils \
           nvme-cli; do
    status=$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)
    if [ "$status" != "install ok installed" ]; then
        echo "t2-purge: $pkg is no longer installed after the purge:" >&2
        echo "  status='$status' - refusing to ship a broken package set" >&2
        exit 1
    fi
    echo "t2-purge: $pkg kept ($status)"
done

# The driver's build-time policy-rc.d (exit 101) keeps these removals from
# starting/stopping anything in the chroot; this hook only edits the dpkg
# database and the on-disk files.
