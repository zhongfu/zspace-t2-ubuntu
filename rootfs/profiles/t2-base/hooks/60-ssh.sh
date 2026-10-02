#!/bin/sh
# Root ssh access by key only.
set -e

install -d -m 700 /root/.ssh
# No key is baked into the image.  The operator's key comes from the config
# partition at provision time (ssh.authorized_key=, applied by
# t2-provision.service) or they log in with the documented root password and
# install one.  If a profile *does* hand us a key (a fleet baking its own), it
# is installed - but the shipped profiles deliberately do not.
if [ -f /t2-profile/ssh/authorized_keys ]; then
    install -m 600 /t2-profile/ssh/authorized_keys /root/.ssh/authorized_keys
else
    rm -f /root/.ssh/authorized_keys
fi

# Make sure the drop-in directory is honoured even on a config that lacks the
# Include line.
if ! grep -q '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config; then
    sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
fi

# Never bake the host keys into the image: they would be identical on every
# flashed board (a fleet-wide secret).  Remove the ones openssh-server's
# postinst generated and let t2-ssh-hostkeys.service run ssh-keygen -A on the
# first boot.  Ubuntu's own sshd-keygen.service cannot do it: it is gated on
# ConditionFirstBoot=yes, which is false once the image carries a machine-id.
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
systemctl enable t2-ssh-hostkeys.service
