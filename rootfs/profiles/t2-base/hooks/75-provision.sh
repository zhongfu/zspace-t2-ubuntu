#!/bin/sh
# Out-of-band provisioning (P3): make the two provisioning scripts executable
# inside the rootfs.  The overlay copy preserves modes, but this is explicit -
# same belt-and-braces as 70-leds.sh.
#
# Both scripts are no-ops when their prerequisite is missing (no `T2-CONFIG`
# partition, no UDC), so this hook and the enabled units are safe on the eMMC
# layout too.
set -e
chmod 755 /usr/local/sbin/t2-provision.sh /usr/local/sbin/t2-usbgadget.sh
