#!/bin/sh
# LED policy scripts from the overlay must be executable inside the rootfs
# (the overlay copy preserves modes, but make it explicit).
#
# Ownership: t2-leds.sh drives the power LEDs; t2-hddled.sh owns the four
# hdd* LEDs.  One owner per LED.
set -e
chmod 755 /usr/local/sbin/t2-leds.sh /usr/local/sbin/t2-hddled.sh
