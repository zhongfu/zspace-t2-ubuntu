#!/bin/sh
# Drop cached .debs and the build-time policy hook, so the image boots with a
# normal apt policy-rc.d (services installed later may start normally).
set -e
apt-get clean
rm -f /usr/sbin/policy-rc.d
sync
