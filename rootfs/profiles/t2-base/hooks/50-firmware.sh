#!/bin/sh
# WiFi / Bluetooth firmware, plus the RTL8156B NIC's firmware patch.
#
# The AP6275P (BCM43752) blob is a Broadcom/Cypress part that only Rockchip and
# AMPak ship - it is NOT in linux-firmware.  brcmfmac needs the .bin (mandatory)
# plus .txt; the .clm_blob is optional.  The module probes at ~7 s, before
# switch_root, so the same files must also live in the kernel's embedded
# initramfs (workbench/initramfs-bringup/) - this hook only covers the rootfs.
#
# Firmware provenance (notes/distro-image.md, patches/README.md):
#   fw_bcm43752a2_pcie_ag.bin -> brcmfmac43752-pcie.bin
#   nvram_AP6275P.txt         -> brcmfmac43752-pcie.txt
#   clm_bcm43752a2_ag.blob    -> brcmfmac43752-pcie.clm_blob
set -e

mkdir -p /lib/firmware/brcm
cp -f /t2-profile/firmware/brcm/brcmfmac43752-pcie.bin \
      /t2-profile/firmware/brcm/brcmfmac43752-pcie.txt \
      /t2-profile/firmware/brcm/brcmfmac43752-pcie.clm_blob \
      /lib/firmware/brcm/
# Bluetooth .hcd images (brcmfmac's BT side loads them via btattach).
cp -f /t2-profile/firmware/brcm/BCM*.hcd /lib/firmware/brcm/ 2>/dev/null || true
chmod 644 /lib/firmware/brcm/* 2>/dev/null || true
ls -l /lib/firmware/brcm

# The onboard 2.5GbE NIC behind the RJ-45 is a Realtek RTL8156B (USB, on the
# board's own VL817 hub), and r8152 wants its firmware patch: without it the
# driver loads but logs "Direct firmware load for rtl_nic/rtl8156b-2.fw
# failed" and runs unpatched.
mkdir -p /lib/firmware/rtl_nic
cp -f /t2-profile/firmware/rtl_nic/*.fw /lib/firmware/rtl_nic/
chmod 644 /lib/firmware/rtl_nic/* 2>/dev/null || true
ls -l /lib/firmware/rtl_nic
