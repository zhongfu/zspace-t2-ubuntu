#!/bin/sh
# Consoles: serial on ttyS2 (1500000n8, the board's console UART) and HDMI on
# tty1, both plain password logins (no autologin).
# Bring-up posture (Raspbian-style): root is locked in the base tarball and
# the image carries no WiFi credentials (P3's job) and no other user, so the
# only guaranteed way in is a documented static root password on these two
# consoles.  Change it on first login (`passwd`); real credentials arrive
# with P3 provisioning.  ssh stays key-only (hooks/60-ssh.sh).
set -e

systemctl enable getty@tty1.service
systemctl enable serial-getty@ttyS2.service

# tty0 aliases the *active* VT - enabling it collides with tty1.
systemctl disable getty@tty0.service 2>/dev/null || true

# No autologin anywhere: a silent passwordless root is easy to miss; a login
# prompt with a documented password is not.  Drop any autologin drop-in an
# earlier build may have left behind.
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true

# The kernel cmdline carries console=ttyS2,1500000n8; agetty needs the speed.
mkdir -p /etc/systemd/system/serial-getty@ttyS2.service.d
cat > /etc/systemd/system/serial-getty@ttyS2.service.d/baud.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -- \\u' --keep-baud 1500000,115200,57600,38400,9600 %I $TERM
EOF

# Documented static root password for bring-up.  chpasswd only rewrites
# /etc/shadow - no daemon, no network.
printf 'root:%s\n' "$T2_ROOT_PASSWORD" | chpasswd

# Say the password on the consoles themselves so it is impossible to miss.
cat > /etc/issue <<'EOF'
ZSpace T2 (\l) - login as root, password: t2 - change it with `passwd`.
EOF
cp /etc/issue /etc/motd
