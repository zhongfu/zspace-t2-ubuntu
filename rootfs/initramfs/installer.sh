#!/bin/sh
# ZSpace T2 installer initramfs ("flash mode").
#
# This is the busybox-side replacement for the rootfs units t2-install.service
# and t2-flash.service (whose drivers are distro/profiles/t2-base/overlay/
# usr/local/sbin/t2-install.sh and t2-flash.sh).  /init runs this script, as
# PID 1, only when the kernel cmdline carries t2.mode=flash, so a normal boot
# never reaches it.  It is deliberately self-contained: the initramfs has no
# systemd, no udev, no findmnt, no sfdisk and no evtest, so everything it needs
# is a busybox applet or one of the two static helpers in bin/ (t2-keywait,
# zstd).
#
# Two flows, selected by which key set the card arms:
#
#   install.*  - write ONE partition (the eMMC image carried in T2-INSTALL).
#   flash.*    - repartition the eMMC and rewrite rootfs + boot tree + loader:
#                the rootfs image comes from the T2-FLASH payload ext4, while
#                the boot tree (Image, dtb, extlinux/, uboot.env, Image.old)
#                and the loader files come as *files* from the card's config
#                FAT - no nested boot-image anywhere.
#
# The safety invariants of both rootfs drivers are preserved here, because a
# boot script that writes a disk gets exactly one chance to be careful:
#
#   * the payload is verified (sha256) *before* the first target write;
#   * the disk we are running from is never a target, and neither is the disk
#     the payload lives on (that would destroy the source mid-write);
#   * size and compression-magic/`-t` mismatches are refused, never guessed at;
#   * in flash mode the old oem is backed up to the card before the table
#     changes, the new oem is created at a fresh LBA, and the boot chain
#     (rootfs -> boot tree -> U-Boot -> SPL) is written last;
#   * LED feedback (red while writing, green when done);
#   * not armed is a clean stop: the board drops to the recovery shell.
#
# Config keys (card FAT partition, `t2-config.txt`; exactly the names and
# semantics t2-provision.sh applies):
#
#   install.presses=3            required to arm
#   install.payload=T2-INSTALL   partition label holding the image
#   install.file=/t2-install.img file inside that partition
#   install.sha256=<hex>         verified before anything is written
#   install.target=/dev/mmcblk0  the disk/partition to overwrite
#   install.window=20            seconds allowed for the presses
#   install.button=rk805 pwrkey  input device name
#   install.key=116              KEY_POWER
#   install.decompress=zstd      stream zstd-compressed payload through `zstd -dc`
#
#   flash.presses=3                required to arm
#   flash.payload=T2-FLASH         partition label holding the payload ext4
#   flash.rootfs=/rootfs.ext4.zst  file in that partition (default)
#   flash.sha256=<hex>             verified before anything is written
#   flash.tree=<dir>               root of the boot tree (default: the mounted
#                                  card FAT root); Image, the dtb,
#                                  extlinux/t2-emmc.conf and uboot.env live there
#   flash.uboot=u-boot.itb         our U-Boot FIT (at the tree root), to p1
#   flash.idbloader=idbloader.img  our SPL (at the tree root) at LBA 0x40;
#                                  `none` keeps the vendor SPL
#   flash.disk=/dev/mmcblk0        the disk to repartition
#   flash.window=60                seconds allowed for the presses
#   flash.button=rk805 pwrkey      input device name
#   flash.key=116                  KEY_POWER
#   flash.decompress=zstd          stream the rootfs through `zstd -dc`
#
# Test hooks (the same pattern as the rootfs drivers):
#   T2_INSTALL_ROOT             relocate every path written
#   T2_INSTALL_CONFIG_DIR       skip the mount, point at a dir with t2-config.txt
#   T2_INSTALL_CONFIG_LABEL     override the config partition labels
#   T2_INSTALL_SHELL            program used for the recovery shell
#   T2_INSTALL_KEYWAIT          alternate press counter
#   T2_INSTALL_ROOTDEV / T2_FLASH_ROOTDEV   override "which disk holds this rootfs"
#   T2_INSTALL_NOREBOOT=1 / T2_FLASH_NOREBOOT=1   do not reboot at the end
#   T2_INSTALL_DRYRUN=1 / T2_FLASH_DRYRUN=1       stop before the first write
#   T2_INSTALL_FORCE=1 / T2_FLASH_FORCE=1         treat the press count as met
#   T2_INSTALL_LEDS / _LED_RED / _LED_GREEN       the status LEDs
#   T2_INSTALL_LED_LOG=1        log every LED brightness write (schedule test)
#   T2_INSTALLER_GEOM_*         override the flash geometry (planning smoke)
set -u

ROOT=${T2_INSTALL_ROOT:-/}
LOG="$ROOT/var/log/t2-installer.log"
TMP="$ROOT/tmp/t2-installer"
SAY="t2-installer"
LABELS=${T2_INSTALL_CONFIG_LABEL:-"T2-CONFIG t2-config"}
CONFIG_DIR=${T2_INSTALL_CONFIG_DIR:-}
SHELL_BIN=${T2_INSTALL_SHELL:-busybox}
KEYWAIT=${T2_INSTALL_KEYWAIT:-t2-keywait}

# The status LEDs are the power LEDs t2-leds.sh would own on a normal boot;
# here nothing else touches them, so the installer owns them outright.
LEDS=${T2_INSTALL_LEDS:-/sys/class/leds}
LED_RED=${T2_INSTALL_LED_RED:-power-led-red}
LED_GREEN=${T2_INSTALL_LED_GREEN:-power-led-green}
blinker=''
CFG_MNT=''
PAY_MNT=''
BMNT=''

# Flash geometry, fixed (a repartition is the point).  p1 must start at LBA
# 0x4000 because that is the SPL's fixed U-Boot offset; the new oem at 0x6000
# does not overlap the vendor's at 0x1c48000, which is what makes
# "create beside it, copy, then repartition" safe.
GEOM_UBOOT_START=${T2_INSTALLER_GEOM_UBOOT_START:-16384}	# 0x4000, 8 MiB
GEOM_UBOOT_SECTORS=${T2_INSTALLER_GEOM_UBOOT_SECTORS:-8192}	# 4 MiB
GEOM_OEM_START=${T2_INSTALLER_GEOM_OEM_START:-24576}		# 0x6000, 12 MiB
GEOM_OEM_SECTORS=${T2_INSTALLER_GEOM_OEM_SECTORS:-262144}	# 128 MiB
GEOM_BOOT_START=${T2_INSTALLER_GEOM_BOOT_START:-286720}		# 0x46000, 140 MiB
GEOM_BOOT_SECTORS=${T2_INSTALLER_GEOM_BOOT_SECTORS:-524288}	# 256 MiB
GEOM_ROOTFS_START=${T2_INSTALLER_GEOM_ROOTFS_START:-811008}	# 0xC6000, 396 MiB

# GPT type GUIDs and pinned unique GUIDs.  On-disk GPT GUIDs are mixed-endian:
# the first three fields are little-endian, the last eight bytes raw, so the
# canonical strings below are converted by uuid_hex().  The unique GUIDs are
# the partition PARTUUIDs Linux reports (and what a `root=PARTUUID=` / fstab
# line names), so they are pinned, not minted:
#
#   p4 rootfs  = the value distro/profiles/t2-base/hooks/20-basics.sh bakes
#                into /etc/fstab (image.json root.partuuid), so the flashed
#                rootfs keeps a valid fstab;
#   p1/p2/p3   = fixed so a rebuilt table is deterministic/reproducible.
TYPE_LINUX_HEX=af3dc60f838472478e793d69d8477de4
TYPE_FAT_HEX=a2a0d0ebe5b9334487c068b6b72699c7
# The boot partition MUST carry the EFI System Partition type GUID, not plain
# FAT (basic data).  mainline U-Boot's bootstd only ever scans partitions it
# considers bootable: `bootflow scan` sets BOOTFLOWIF_ONLY_BOOTABLE, and
# disk/part_efi.c's get_bootable() reports PART_EFI_SYSTEM_PARTITION for an ESP
# type GUID (or PART_BOOTABLE for the legacy-BIOS-bootable attribute).  With
# no bootable partition, part_get_bootable() returns 0 and the iterator scans
# only partition 1 - the raw loader - so the scan reports "0 bootflows" and
# drops to the U-Boot prompt.  This was the installed eMMC's failure on
# 2026-10-02; scripts/t2-image.py has always used CONFIG_TYPE_GUID (the same
# ESP GUID) for the tree partition, which is why the card and the bring-up
# trees booted and the installer's own layout did not.
TYPE_ESP_HEX=28732ac11ff8d211ba4b00a0c93ec93b
DISK_GUID_HEX=5a5b5c5d5e5f50515253545556575870
U_UBOOT_UUID=614e0000-0000-4b53-8000-1d28000054a1
U_OEM_UUID=614e0000-0000-4b53-8000-1d28000054a2
U_BOOT_UUID=614e0000-0000-4b53-8000-1d28000054a3
U_ROOTFS_UUID=614e0000-0000-4b53-8000-1d28000054a9

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
log() {
	mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
	echo "$(date -Is) $SAY: $*" >>"$LOG" 2>/dev/null || true
	echo "$SAY: $*"
}

led_set() { # $1 = LED name, $2 = 0/1
	dir="$LEDS/$1"
	[ -d "$dir" ] || return 0
	echo none > "$dir/trigger" 2>/dev/null || true
	echo "$2" > "$dir/brightness" 2>/dev/null || true
	# Test hook: mirror every brightness write into the log so the schedule can
	# be asserted from the outside (off in production).
	[ -z "${T2_INSTALL_LED_LOG:-}" ] || log "led $1=$2"
}

led_claim() {
	# Take both power LEDs over from the kernel's gpio-leds defaults.  U-Boot
	# left red on / green off, but Linux's gpio-leds probe re-applies the DT
	# `default-state` when it registers (green "on" with trigger "default-on"),
	# so without this green stays lit and drowns out the red press cadence -
	# measured on hardware 2026-10-02, the gate looked like a solid green LED.
	led_set "$LED_GREEN" 0
	led_set "$LED_RED" 0
}

blink() { # $1 = LED name, $2 = times, $3 = half-period, $4 = state to leave it in
	[ -d "$LEDS/$1" ] || return 0
	i=0
	while [ "$i" -lt "$2" ]; do
		led_set "$1" 1
		sleep "$3"
		led_set "$1" 0
		sleep "$3"
		i=$((i + 1))
	done
	led_set "$1" "$4"
}

cleanup() {
	if [ -n "$PAY_MNT" ]; then umount "$PAY_MNT" 2>/dev/null || true; fi
	if [ -n "$BMNT" ]; then umount "$BMNT" 2>/dev/null || true; fi
	if [ -n "$CFG_MNT" ]; then umount "$CFG_MNT" 2>/dev/null || true; fi
	[ -z "$blinker" ] || kill "$blinker" 2>/dev/null || true
	blinker=''
	led_set "$LED_RED" 0
}

drop_to_shell() {
	cleanup
	log "dropping to the recovery shell on the console"
	exec "$SHELL_BIN" sh -i
}

# find a partition by filesystem label.  busybox blkid has no -t/-o, so probe
# every block device the way /init resolves root=.  busybox prints
# `LABEL="..." UUID="..." TYPE="..."` per device (no device name); a
# `device: LABEL=...` form is also accepted, which is what the host test shim
# emits so it can control the resolved path.
label_dev() { # $1 = label -> device on stdout
	want=$1
	for s in /sys/class/block/*; do
		case "${s##*/}" in loop*|ram*|zram*|*boot0|*boot1) continue ;; esac
		d=/dev/${s##*/}
		[ -b "$d" ] || continue
		out=$(blkid "$d" 2>/dev/null || true)
		case "$out" in
		*"LABEL=\"$want\""*)
			case "$out" in
			*:*) printf '%s\n' "${out%%:*}" ;;
			*) printf '%s\n' "$d" ;;
			esac
			return 0
			;;
		esac
	done
	return 1
}

# partition or whole disk -> the whole disk
disk_of() {
	p=$(readlink -f "$1" 2>/dev/null || echo "$1")
	case "$p" in
	/dev/mmcblk*p[0-9]*) echo "${p%p[0-9]*}" ;;
	/dev/nvme*n[0-9]p[0-9]*) echo "${p%p[0-9]*}" ;;
	/dev/sd*[0-9]) echo "${p%[0-9]}" ;;
	*) echo "$p" ;;
	esac
}

cmdline_root() {
	for w in $(cat /proc/cmdline 2>/dev/null); do
		case "$w" in root=*) echo "${w#root=}" ;; esac
	done
}

resolve_spec() { # $1 = /dev/... or LABEL=... or UUID=...
	case "$1" in
	/dev/*) printf '%s\n' "$1" ;;
	LABEL=*) label_dev "${1#LABEL=}" ;;
	UUID=*)
		want=${1#UUID=}
		for s in /sys/class/block/*; do
			case "${s##*/}" in loop*|ram*|zram*) continue ;; esac
			d=/dev/${s##*/}
			[ -b "$d" ] || continue
			out=$(blkid "$d" 2>/dev/null || true)
			got=$(printf '%s' "$out" | sed -n 's/.*[[:space:]]UUID="\([^"]*\)".*/\1/p')
			[ -n "$got" ] || got=$(printf '%s' "$out" | sed -n 's/^UUID="\([^"]*\)".*/\1/p')
			[ "$got" = "$want" ] && { printf '%s\n' "$d"; return 0; }
		done
		return 1 ;;
	*) return 1 ;;
	esac
}

# "which disk holds this rootfs".  This runs *before* any rootfs is mounted (by
# construction, /init never mounts one in flash mode), so the honest answer is
# usually "none".  We still resolve root= from the cmdline when it names a
# block device, because a wrongly configured target must not overwrite the
# medium this initramfs was loaded from.
root_dev() {
	[ -n "${T2_INSTALL_ROOTDEV:-}" ] && { printf '%s\n' "$T2_INSTALL_ROOTDEV"; return 0; }
	[ -n "${T2_FLASH_ROOTDEV:-}" ] && { printf '%s\n' "$T2_FLASH_ROOTDEV"; return 0; }
	if command -v findmnt >/dev/null 2>&1; then
		findmnt -n -o SOURCE "$ROOT/" 2>/dev/null || true
		return 0
	fi
	resolve_spec "$(cmdline_root)"
}

arm_ok() { # $1 = presses value, $2 = window value, $3 = what is being armed
	if ! printf '%s' "$1" | grep -Eq '^[0-9]+$'; then
		log "$3 presses '$1' is not a number; refusing to arm"
		return 1
	fi
	if [ -n "$2" ] && ! printf '%s' "$2" | grep -Eq '^[0-9]+$'; then
		log "$3 window '$2' is not a number; refusing to arm"
		return 1
	fi
	return 0
}

# The red-LED gate cadence, driven from t2-keywait's status file.  POSIX sh has
# no floats, so the half-period is tracked in tenths: 1.0 s before the first
# press, 0.1 s less after each counted press (floor at 0).  A 3-press target
# runs 1.0/1.0, 0.9/0.9, 0.8/0.8, then solid red; a 10-press target reaches
# 0.1/0.1 at the ninth press and solid red at the tenth.
blink_gate() { # background: blink red at the cadence read from the status file
	led_set "$LED_RED" 0
	while :; do
		n=$(cat "$status" 2>/dev/null || echo 0)
		case "$n" in '' | *[!0-9]*) n=0 ;; esac
		h=$((10 - n))
		# Floor the half-period at 0.1 s: at the target count this computes to
		# 0, and `sleep 0.0` does not sleep at all - the blinker then toggles
		# the LED as fast as the shell can, which looks like a *dim* red LED
		# instead of the solid red that follows (measured on hardware
		# 2026-10-02: "after the 10th press the LED was dim red, very high
		# on/off frequency").
		[ "$h" -lt 1 ] && h=1
		half=$(printf '%d.%d' $((h / 10)) $((h % 10)))
		led_set "$LED_RED" 1
		sleep "$half"
		led_set "$LED_RED" 0
		sleep "$half"
	done
}

count_presses() { # $1 window, $2 presses, $3 key, $4 button, $5 force-var value
	if [ "$5" = 1 ]; then
		log "test hook: press gate satisfied (forced)"
		return 0
	fi
	log "counting presses: press the power button $2 time(s) within ${1}s (red LED: 1.0s on/off, -0.1s per press)"
	led_claim
	status="$TMP/press-count"
	: > "$status"
	blinker=''
	blink_gate &
	blinker=$!
	"$KEYWAIT" "$1" "$2" "$3" "$4" "$status"
	rc=$?
	kill "$blinker" 2>/dev/null || true
	wait "$blinker" 2>/dev/null || true
	blinker=''
	led_set "$LED_RED" 0
	if [ "$rc" -eq 0 ]; then
		# The target's natural interval is 0, so hold solid red for 5 s.
		log "press count confirmed ($2); red solid 5s before writing"
		led_set "$LED_RED" 1
		sleep 5
		return 0
	fi
	return 1
}

reboot_now() {
	# `reboot -f` still goes through the kernel's device_shutdown, and that path
	# *stalls* on this board (item 4: RCU stall with the CPUs idle, watchdog
	# reset later - or nothing at all when no watchdog is armed).  Measured
	# 2026-10-02: the flash flow logged "flash complete; rebooting" and the board
	# sat there drawing 4.76 W, never resetting.  SysRq-b is the *emergency*
	# restart: it skips device_shutdown entirely and has always worked here (the
	# bring-up notes use it for exactly this reason), so try it first.
	#
	# Guarded on `$$ -eq 1`: /proc/sysrq-trigger is host state, and this script
	# is also exercised by test-t2-installer.sh on the workstation - a root-run
	# test must never reboot the workstation.  (In the initramfs /init `exec`s
	# installer.sh, so it really is PID 1 there.)
	if [ "$$" -eq 1 ] && [ -z "${T2_INSTALL_NO_SYSRQ:-}" ]; then
		echo 1 > /proc/sys/kernel/sysrq 2>/dev/null || true
		echo b > /proc/sysrq-trigger 2>/dev/null || true
		sleep 2
	fi
	reboot -f || { log "reboot failed"; drop_to_shell; }
}

# ---------------------------------------------------------------------------
# GPT writer (busybox has no sfdisk; fdisk in this build cannot do GPT, so the
# 512-byte header + the 128-entry array are built with printf/xxd and their
# CRC32s with the busybox crc32 applet).
# ---------------------------------------------------------------------------
le32() { # $1 = value -> 8 hex chars, little-endian
	n=$1
	i=0
	s=''
	while [ "$i" -lt 4 ]; do
		s="$s$(printf '%02x' $((n & 255)))"
		n=$((n >> 8))
		i=$((i + 1))
	done
	printf '%s' "$s"
}

le64() { # $1 = value -> 16 hex chars, little-endian
	n=$1
	i=0
	s=''
	while [ "$i" -lt 8 ]; do
		s="$s$(printf '%02x' $((n & 255)))"
		n=$((n >> 8))
		i=$((i + 1))
	done
	printf '%s' "$s"
}

zeros() { # $1 = byte count -> that many 00 hex pairs
	i=0
	s=''
	while [ "$i" -lt "$1" ]; do
		s="${s}00"
		i=$((i + 1))
	done
	printf '%s' "$s"
}

revhex() { # reverse the byte pairs of a hex string (crc32 prints big-endian;
	# GPT header fields are little-endian uint32)
	h=$1
	out=''
	i=${#h}
	while [ "$i" -gt 0 ]; do
		i=$((i - 2))
		out="$out${h:$i:2}"
	done
	printf '%s' "$out"
}

utf16le_hex() { # $1 = ASCII name -> 72-byte UTF-16LE field as hex
	hx=$(printf '%s' "$1" | od -An -tx1 | tr -d ' \n')
	out=''
	i=0
	while [ "$i" -lt "${#hx}" ]; do
		out="$out${hx:$i:2}00"
		i=$((i + 2))
	done
	while [ "${#out}" -lt 144 ]; do
		out="${out}00"
	done
	printf '%s' "$out"
}

uuid_hex() { # $1 = canonical UUID -> 32 hex chars in GPT on-disk (mixed-endian) order
	h=$(printf '%s' "$1" | tr -d '-')
	printf '%s' \
		"${h:6:2}${h:4:2}${h:2:2}${h:0:2}" \
		"${h:10:2}${h:8:2}" \
		"${h:14:2}${h:12:2}" \
		"${h:16:16}"
}

write_entry() { # idx type_hex unique_hex first_lba last_lba name_hex outfile
	hx=$(printf '%s' "$2" "$3" "$(le64 "$4")" "$(le64 "$5")" \
		"0000000000000000" "$6")
	printf '%s' "$hx" | xxd -r -p > "$TMP/gpt-entry.bin" || return 1
	dd if="$TMP/gpt-entry.bin" of="$7" bs=128 seek="$1" conv=notrunc 2>/dev/null \
		|| return 1
	return 0
}

write_gpt() { # $1 = disk, $2 = sector count
	wg_disk=$1
	wg_last=$(($2 - 1))
	[ "$wg_last" -gt 34 ] || return 1

	# --- protective MBR (LBA 0) --------------------------------------------
	# Layout: 440 bytes boot code, 6 reserved bytes, one 16-byte entry
	# (type 0xEE covering the disk), 3 empty entries, then 0x55AA.
	mbr_size=$((wg_last))
	[ "$mbr_size" -gt 4294967295 ] && mbr_size=4294967295
	mbr_hx=$(printf '%s' "$(zeros 440)" "$(zeros 6)" "00" "000200" "ee" \
		"ffffff" "$(le32 1)" "$(le32 "$mbr_size")" "$(zeros 48)" "55aa")
	printf '%s' "$mbr_hx" | xxd -r -p > "$TMP/gpt-mbr.bin" || return 1
	dd if="$TMP/gpt-mbr.bin" of="$wg_disk" bs=512 seek=0 conv=fsync 2>/dev/null \
		|| return 1

	# --- 128 x 128-byte partition entry array ------------------------------
	: > "$TMP/gpt-entries.bin"
	dd if=/dev/zero of="$TMP/gpt-entries.bin" bs=512 count=32 2>/dev/null \
		|| return 1
	write_entry 0 "$TYPE_LINUX_HEX" "$(uuid_hex "$U_UBOOT_UUID")" \
		"$GEOM_UBOOT_START" "$((GEOM_UBOOT_START + GEOM_UBOOT_SECTORS - 1))" \
		"$(utf16le_hex uboot)" "$TMP/gpt-entries.bin" || return 1
	write_entry 1 "$TYPE_LINUX_HEX" "$(uuid_hex "$U_OEM_UUID")" \
		"$GEOM_OEM_START" "$((GEOM_OEM_START + GEOM_OEM_SECTORS - 1))" \
		"$(utf16le_hex oem)" "$TMP/gpt-entries.bin" || return 1
	write_entry 2 "$TYPE_ESP_HEX" "$(uuid_hex "$U_BOOT_UUID")" \
		"$GEOM_BOOT_START" "$((GEOM_BOOT_START + GEOM_BOOT_SECTORS - 1))" \
		"$(utf16le_hex boot)" "$TMP/gpt-entries.bin" || return 1
	wg_rootfs_end=$((wg_last - 33))
	write_entry 3 "$TYPE_LINUX_HEX" "$(uuid_hex "$U_ROOTFS_UUID")" \
		"$GEOM_ROOTFS_START" "$wg_rootfs_end" \
		"$(utf16le_hex rootfs)" "$TMP/gpt-entries.bin" || return 1

	wg_ecrc=$(crc32 "$TMP/gpt-entries.bin" | cut -d' ' -f1)

	# --- GPT header (LBA 1 primary, last sector backup) --------------------
	build_gpt_hdr() { # $1 = crc32 hex, $2 = current_lba, $3 = backup_lba
		printf '%s' "4546492050415254" "00000100" "5c000000" \
			"$(revhex "$1")" "00000000" \
			"$(le64 "$2")" "$(le64 "$3")" "$(le64 34)" \
			"$(le64 $((wg_last - 33)))" "$DISK_GUID_HEX" \
			"$(le64 2)" "80000000" "80000000" "$(revhex "$wg_ecrc")"
	}
	printf '%s' "$(build_gpt_hdr 00000000 1 "$wg_last")" | xxd -r -p \
		> "$TMP/gpt-hdr.bin" || return 1
	wg_hcrc=$(crc32 "$TMP/gpt-hdr.bin" | cut -d' ' -f1)
	printf '%s' "$(build_gpt_hdr "$wg_hcrc" 1 "$wg_last")" | xxd -r -p \
		> "$TMP/gpt-hdr-final.bin" || return 1
	printf '%s' "$(build_gpt_hdr 00000000 "$wg_last" 1)" | xxd -r -p \
		> "$TMP/gpt-hdr-backup.bin" || return 1
	wg_hbcrc=$(crc32 "$TMP/gpt-hdr-backup.bin" | cut -d' ' -f1)
	printf '%s' "$(build_gpt_hdr "$wg_hbcrc" "$wg_last" 1)" | xxd -r -p \
		> "$TMP/gpt-hdr-backup-final.bin" || return 1

	# --- write primary + backup, then ask the kernel to rescan -------------
	# conv=notrunc so each seeked write leaves the rest of the table alone;
	# on a real block device O_TRUNC is ignored anyway, but this is explicit.
	dd if="$TMP/gpt-entries.bin" of="$wg_disk" bs=512 seek=2 conv=fsync,notrunc 2>/dev/null \
		|| return 1
	dd if="$TMP/gpt-hdr-final.bin" of="$wg_disk" bs=512 seek=1 conv=fsync,notrunc 2>/dev/null \
		|| return 1
	dd if="$TMP/gpt-entries.bin" of="$wg_disk" bs=512 seek=$((wg_last - 32)) conv=fsync,notrunc 2>/dev/null \
		|| return 1
	dd if="$TMP/gpt-hdr-backup-final.bin" of="$wg_disk" bs=512 seek="$wg_last" conv=fsync,notrunc 2>/dev/null \
		|| return 1
	dd if="$TMP/gpt-mbr.bin" of="$wg_disk" bs=512 seek=0 conv=fsync,notrunc 2>/dev/null \
		|| return 1
	return 0
}

# ---------------------------------------------------------------------------
# install flow (one partition)
# ---------------------------------------------------------------------------
install_run() {
	PRESSES=${install_presses:-}
	WINDOW=${install_window:-20}
	LABEL=${install_payload:-}
	FILE=${install_file:-/t2-install.img}
	WANT_SHA=${install_sha256:-}
	TARGET=${install_target:-/dev/mmcblk0}
	BUTTON=${install_button:-rk805 pwrkey}
	KEY=${install_key:-116}
	DECOMPRESS=${install_decompress:-}

	log "armed: $PRESSES press(es) within ${WINDOW}s confirms writing $LABEL:$FILE to $TARGET"
	blink "$LED_GREEN" 3 0.2 1

	if [ -z "$LABEL" ]; then
		log "no install.payload label; nothing to install"
		drop_to_shell
	fi
	dev=$(label_dev "$LABEL" || true)
	if [ -z "$dev" ]; then
		log "no '$LABEL' partition - was the card inserted?"
		drop_to_shell
	fi
	PAY_MNT="$ROOT/run/t2-installer-payload"
	mkdir -p "$PAY_MNT"
	if ! mount -t ext4 -o ro "$dev" "$PAY_MNT"; then
		log "cannot mount $dev read-only"
		drop_to_shell
	fi

	payload="$PAY_MNT$FILE"
	if [ ! -f "$payload" ]; then
		log "no $FILE in $LABEL"
		drop_to_shell
	fi
	size=$(stat -c %s "$payload" 2>/dev/null || echo 0)
	target_size=$(blockdev --getsize64 "$TARGET" 2>/dev/null || echo 0)
	if [ "$size" -eq 0 ] || [ "$target_size" -eq 0 ]; then
		log "cannot size $payload or $TARGET"
		drop_to_shell
	fi
	if [ -z "$DECOMPRESS" ] && [ "$size" -gt "$target_size" ]; then
		log "payload is $size bytes, target is only $target_size; refusing"
		drop_to_shell
	fi
	case "$DECOMPRESS" in
	'' | zstd) ;;
	*)
		log "install.decompress='$DECOMPRESS' is not supported (raw or zstd)"
		drop_to_shell
		;;
	esac

	# Never write the system we are running from, never write the payload's own
	# disk: both destroy the ability to finish or retry.
	rd=$(root_dev)
	if [ -n "$rd" ] && [ "$(disk_of "$rd")" = "$(disk_of "$TARGET")" ]; then
		log "target $(disk_of "$TARGET") holds this rootfs; refusing"
		drop_to_shell
	fi
	if [ "$(disk_of "$dev")" = "$(disk_of "$TARGET")" ]; then
		log "payload lives on the target disk; refusing"
		drop_to_shell
	fi

	if [ -n "$WANT_SHA" ]; then
		got=$(sha256sum "$payload" | cut -d' ' -f1)
		if [ "$got" != "$WANT_SHA" ]; then
			log "payload sha256 $got != $WANT_SHA; refusing"
			drop_to_shell
		fi
		log "payload sha256 verified ($got)"
	fi

	# The write cannot catch a corrupt stream (`zstd -dc f | dd` reports only
	# dd's status), so test the stream first.
	if [ "$DECOMPRESS" = zstd ]; then
		if ! zstd -t "$payload" >/dev/null 2>&1; then
			log "payload failed its zstd integrity test; refusing"
			drop_to_shell
		fi
		log "payload zstd stream verified"
	fi

	if ! count_presses "$WINDOW" "$PRESSES" "$KEY" "$BUTTON" "${T2_INSTALL_FORCE:-}"; then
		log "press count not confirmed ($PRESSES required); nothing written"
		drop_to_shell
	fi

	log "confirmed: writing $(basename "$payload") to $TARGET${DECOMPRESS:+ (zstd)}"
	if [ "${T2_INSTALL_DRYRUN:-}" = 1 ]; then
		blink "$LED_RED" 2 0.2 0
		if [ -n "$DECOMPRESS" ]; then
			log "dry run: would run zstd -dc $payload | dd of=$TARGET bs=4M conv=fsync"
		else
			log "dry run: would run dd if=$payload of=$TARGET bs=4M conv=fsync"
		fi
		drop_to_shell
	fi

	( while :; do
		led_set "$LED_RED" 1; led_set "$LED_GREEN" 0; sleep 0.25
		led_set "$LED_RED" 0; led_set "$LED_GREEN" 1; sleep 0.25
	  done ) &
	blinker=$!
	rc=0
	if [ -n "$DECOMPRESS" ]; then
		zstd -dc "$payload" | dd of="$TARGET" bs=4M conv=fsync 2>/dev/null || rc=$?
	else
		dd if="$payload" of="$TARGET" bs=4M conv=fsync 2>/dev/null || rc=$?
	fi
	if [ "$rc" != 0 ]; then
		log "dd failed; the target may be in an unknown state - reinstall from the card"
		drop_to_shell
	fi
	kill "$blinker" 2>/dev/null || true
	wait "$blinker" 2>/dev/null || true
	blinker=''
	led_set "$LED_RED" 0
	led_set "$LED_GREEN" 1
	sync
	log "install complete; rebooting into the written system (remove the card first if this boot came from it)"
	if [ "${T2_INSTALL_NOREBOOT:-}" = 1 ]; then
		log "reboot suppressed (test); the board would reboot now"
		drop_to_shell
	fi
	reboot_now
}

# ---------------------------------------------------------------------------
# flash flow (repartition + rewrite)
# ---------------------------------------------------------------------------
flash_run() {
	PRESSES=${flash_presses:-}
	WINDOW=${flash_window:-60}
	LABEL=${flash_payload:-T2-FLASH}
	ROOTFS=${flash_rootfs:-/rootfs.ext4.zst}
	WANT_SHA=${flash_sha256:-}
	TREE=${flash_tree:-${cfgdir:-}}
	UBOOT_FILE=${flash_uboot:-u-boot.itb}
	IDB=${flash_idbloader:-idbloader.img}
	DISK=${flash_disk:-/dev/mmcblk0}
	BUTTON=${flash_button:-rk805 pwrkey}
	KEY=${flash_key:-116}
	DECOMPRESS=${flash_decompress:-}
	# flash.rootfs names a path inside the payload ext4, so a bare file name
	# means the filesystem root.
	case "$ROOTFS" in /*) ;; *) ROOTFS="/$ROOTFS" ;; esac

	log "armed: $PRESSES press(es) within ${WINDOW}s confirms repartitioning $DISK"

	# --- payload tree -------------------------------------------------------
	dev=$(label_dev "$LABEL" || true)
	if [ -z "$dev" ]; then
		log "no partition labelled $LABEL on the card"
		drop_to_shell
	fi
	# Read-write: the old oem backup is written back to this same partition.
	PAY_MNT="$ROOT/run/t2-installer-payload"
	mkdir -p "$PAY_MNT"
	if ! mount -t ext4 "$dev" "$PAY_MNT"; then
		log "cannot mount $dev"
		drop_to_shell
	fi

	rootfs="$PAY_MNT$ROOTFS"
	if [ -z "$TREE" ] || [ ! -d "$TREE" ]; then
		log "no boot tree root (flash.tree is unset and there is no mounted card FAT)"
		drop_to_shell
	fi
	# The dtb is whatever the eMMC descriptor's `fdt` line names, so the
	# installer copies exactly the file that descriptor boots - never a guess.
	dtb=$(sed -n 's/^[[:space:]]*fdt[[:space:]]*//p' \
		"$TREE/extlinux/t2-emmc.conf" 2>/dev/null | head -n 1)
	dtb=${dtb#/}
	for f in "$rootfs" "$TREE/Image" "$TREE/extlinux/t2-emmc.conf" "$TREE/$UBOOT_FILE"; do
		if [ ! -f "$f" ]; then
			log "no $(basename "$f") under $TREE"
			drop_to_shell
		fi
	done
	if [ -z "$dtb" ] || [ ! -f "$TREE/$dtb" ]; then
		log "the tree's extlinux/t2-emmc.conf does not name a dtb at its root"
		drop_to_shell
	fi
	if [ "$IDB" != none ] && [ ! -f "$TREE/$IDB" ]; then
		log "no $IDB under $TREE (use flash.idbloader=none to keep the vendor SPL)"
		drop_to_shell
	fi
	case "$DECOMPRESS" in
	'' | zstd) ;;
	*)
		log "flash.decompress='$DECOMPRESS' is not supported (raw or zstd)"
		drop_to_shell
		;;
	esac

	# The sha256 is over the file as carried, so the magic decides whether the
	# write would stream it raw or not - never the file name.
	magic=$(head -c 4 "$rootfs" 2>/dev/null | od -An -tx1 | tr -d ' \n')
	if [ "$magic" = 28b52ffd ] && [ "$DECOMPRESS" != zstd ]; then
		log "rootfs is zstd-compressed but flash.decompress is not zstd; refusing (it would be written raw)"
		drop_to_shell
	fi
	if [ "$magic" != 28b52ffd ] && [ "$DECOMPRESS" = zstd ]; then
		log "rootfs is not zstd-compressed but flash.decompress=zstd; refusing"
		drop_to_shell
	fi
	if [ "$DECOMPRESS" = zstd ]; then
		if ! zstd -t "$rootfs" >/dev/null 2>&1; then
			log "rootfs failed its zstd integrity test; refusing"
			drop_to_shell
		fi
		log "rootfs zstd stream verified"
	fi

	disk_size=$(blockdev --getsize64 "$DISK" 2>/dev/null || echo 0)
	if [ "$disk_size" -eq 0 ]; then
		log "cannot size $DISK"
		drop_to_shell
	fi
	if [ -z "$DECOMPRESS" ]; then
		size=$(stat -c %s "$rootfs" 2>/dev/null || echo 0)
		if [ "$size" -gt "$disk_size" ]; then
			log "rootfs is $size bytes, target is only $disk_size; refusing"
			drop_to_shell
		fi
	fi

	# Never write the system we are running from, never write the payload's own
	# disk: both destroy the ability to finish or retry.
	rd=$(root_dev)
	if [ -n "$rd" ] && [ "$(disk_of "$rd")" = "$(disk_of "$DISK")" ]; then
		log "target $(disk_of "$DISK") holds this rootfs; refusing"
		drop_to_shell
	fi
	if [ "$(disk_of "$dev")" = "$(disk_of "$DISK")" ]; then
		log "payload lives on the target disk; refusing"
		drop_to_shell
	fi

	if [ -n "$WANT_SHA" ]; then
		got=$(sha256sum "$rootfs" | cut -d' ' -f1)
		if [ "$got" != "$WANT_SHA" ]; then
			log "rootfs sha256 $got != $WANT_SHA; refusing"
			drop_to_shell
		fi
		log "rootfs sha256 verified ($got)"
	fi

	# --- how the board was turned on (log only, never a gate) ---------------
	on_source() {
		command -v i2cget >/dev/null 2>&1 || { log "on_source: no i2cget, skipping"; return 0; }
		for bus in /dev/i2c-*; do
			[ -e "$bus" ] || continue
			n=${bus#/dev/i2c-}
			v=$(i2cget -y "$n" 0x20 0xf5 2>/dev/null || true)
			if [ -z "$v" ]; then
				[ -e "/sys/bus/i2c/devices/$n-0020" ] || continue
				log "on_source: rk809 at 0x20 on i2c $n is claimed by the kernel driver; see the boot loader's 'PMIC: RK809 (on=...)' line"
				return 0
			fi
			case "$v" in
			0x40) what="on_plugin (power applied)" ;;
			0x80) what="power button pressed" ;;
			*) what="unrecognised" ;;
			esac
			log "on_source: i2c $n reg 0xf5 = $v -> $what (log only, no gate)"
			return 0
		done
		log "on_source: no rk809 at 0x20 on any i2c bus"
	}
	on_source

	# --- presses ------------------------------------------------------------
	if ! count_presses "$WINDOW" "$PRESSES" "$KEY" "$BUTTON" "${T2_FLASH_FORCE:-}"; then
		log "press count not confirmed ($PRESSES required); not flashing"
		drop_to_shell
	fi

	# --- plan ---------------------------------------------------------------
	sectors=$(blockdev --getsz "$DISK" 2>/dev/null || echo 0)
	rootfs_sectors=$((sectors - GEOM_ROOTFS_START - 34))
	rootfs_sectors=$((rootfs_sectors / 2048 * 2048))
	if [ "$rootfs_sectors" -lt 2048 ]; then
		log "disk is too small for the new layout"
		drop_to_shell
	fi
	log "new table: p1 uboot ${GEOM_UBOOT_START}/$((GEOM_UBOOT_SECTORS / 2048))MiB, p2 oem ${GEOM_OEM_START}/$((GEOM_OEM_SECTORS / 2048))MiB, p3 boot ${GEOM_BOOT_START}/$((GEOM_BOOT_SECTORS / 2048))MiB, p4 rootfs ${GEOM_ROOTFS_START}/$((rootfs_sectors / 2048))MiB"
	log "sequence: back up old oem -> write new table -> create+fill new oem -> rootfs -> boot tree -> loader last"

	if [ "${T2_FLASH_DRYRUN:-}" = 1 ]; then
		log "dry run: stopping before the first write"
		led_set "$LED_GREEN" 1
		drop_to_shell
	fi

	# --- back up the old oem (before the table changes) ---------------------
	# The vendor table plus its copies; by LABEL first (busybox blkid has no
	# PARTLABEL), then by the GPT entry name "oem" (the vendor table calls it
	# that).  If it cannot be found there is nothing to carry across - that is
	# a logged, clean stop, not a reason to guess.
	find_oem() {
		for s in /sys/class/block/*; do
			b=${s##*/}
			case "$b" in
			"${DISK##*/}"p*|"${DISK##*/}"[0-9]*) ;;
			*) continue ;;
			esac
			d=/dev/$b
			[ -b "$d" ] || continue
			out=$(blkid "$d" 2>/dev/null || true)
			got=$(printf '%s' "$out" | sed -n 's/.*LABEL="\([^"]*\)".*/\1/p')
			[ "$got" = oem ] && { printf '%s\n' "$d"; return 0; }
		done
		# GPT name fallback: entry N-1 holds partition N.
		[ -b "$DISK" ] || return 1
		gpt="$TMP/gpt-old-entries.bin"
		dd if="$DISK" of="$gpt" bs=512 skip=2 count=32 2>/dev/null || return 1
		for s in /sys/class/block/*; do
			b=${s##*/}
			case "$b" in
			"${DISK##*/}"p*|"${DISK##*/}"[0-9]*) ;;
			*) continue ;;
			esac
			d=/dev/$b
			[ -b "$d" ] || continue
			n=${b##*p}
			case "$n" in ''|*[!0-9]*) n=${b##*${DISK##*/}} ;; esac
			idx=$((n - 1))
			nm=$(dd if="$gpt" bs=1 skip=$((idx * 128 + 56)) count=72 2>/dev/null | tr -d '\000')
			[ "$nm" = oem ] && { printf '%s\n' "$d"; return 0; }
		done
		return 1
	}
	old_oem=$(find_oem || true)
	if [ -z "$old_oem" ]; then
		log "no oem partition found; nothing to carry across"
	else
		stamp=$(date +%Y%m%d-%H%M%S)
		dst="$PAY_MNT/oem-backup/$stamp"
		oldmnt="$PAY_MNT/old-oem"
		mkdir -p "$dst" "$oldmnt"
		if mount -o ro "$old_oem" "$oldmnt"; then
			(cd "$oldmnt" && tar cf - .) | (cd "$dst" && tar xf -) \
				|| { log "old oem copy failed"; drop_to_shell; }
			umount "$oldmnt"
			log "old oem ($old_oem) copied to $dst on the card (versioned backup)"
		else
			log "cannot mount old oem $old_oem read-only; refusing to repartition"
			drop_to_shell
		fi
	fi

	# --- new partition table ------------------------------------------------
	if ! write_gpt "$DISK" "$sectors"; then
		log "could not write the new GPT; stopping"
		drop_to_shell
	fi
	blockdev --rereadpt "$DISK" 2>/dev/null || true
	# (a `partprobe` call used to follow: util-linux's tool does not exist in the
	# busybox initramfs, and `blockdev --rereadpt` above is the actual mechanism.)
	wait_part() { # $1 = partition device
		[ "${T2_INSTALLER_FAKE_PARTS:-}" = 1 ] && return 0
		i=0
		while [ "$i" -lt 100 ]; do
			[ -b "$1" ] && return 0
			sleep 0.1
			i=$((i + 1))
		done
		return 1
	}
	for p in "${DISK}p1" "${DISK}p2" "${DISK}p3" "${DISK}p4"; do
		wait_part "$p" || { log "partition $p did not appear; stopping"; drop_to_shell; }
	done
	log "wrote the new GPT"

	# --- fill the new oem from the backup -----------------------------------
	# Done before the rootfs write: the rootfs partition spans the sectors the
	# old oem occupied, so its contents must already be safe here.
	mke2fs -F -L oem "${DISK}p2" || { log "mke2fs on ${DISK}p2 failed; stopping"; drop_to_shell; }
	newmnt="$PAY_MNT/new-oem"
	mkdir -p "$newmnt"
	if mount "${DISK}p2" "$newmnt"; then
		if [ -n "$old_oem" ]; then
			(cd "$dst" && tar cf - .) | (cd "$newmnt" && tar xf -) \
				|| { log "new oem copy failed"; drop_to_shell; }
			log "new oem filled from $dst"
		else
			log "new oem is empty (no old oem existed)"
		fi
		umount "$newmnt"
	else
		log "cannot mount the new oem to fill it; stopping"
		drop_to_shell
	fi

	# --- rootfs, boot tree, then the loader last ----------------------------
	( while :; do
		led_set "$LED_RED" 1; led_set "$LED_GREEN" 0; sleep 0.25
		led_set "$LED_RED" 0; led_set "$LED_GREEN" 1; sleep 0.25
	  done ) &
	blinker=$!
	log "writing rootfs to ${DISK}p4${DECOMPRESS:+ (zstd)}"
	if [ -n "$DECOMPRESS" ]; then
		zstd -dc "$rootfs" | dd of="${DISK}p4" bs=4M conv=fsync 2>/dev/null \
			|| { log "rootfs write failed"; drop_to_shell; }
	else
		dd if="$rootfs" of="${DISK}p4" bs=4M conv=fsync 2>/dev/null \
			|| { log "rootfs write failed"; drop_to_shell; }
	fi
	# p3 as a fresh VFAT filesystem holding the boot tree *as files*: the
	# installer no longer dd's a boot image.  mkfs.vfat + cp is the whole
	# write; the copied extlinux.conf is read back and its `default` asserted,
	# so a card whose descriptor is not the eMMC one fails here, not on boot.
	log "formatting ${DISK}p3 (FAT, T2-BOOT) and copying the boot tree"
	if ! mkfs.vfat -n T2-BOOT "${DISK}p3"; then
		log "mkfs.vfat on ${DISK}p3 failed; stopping"
		drop_to_shell
	fi
	BMNT="$ROOT/run/t2-installer-boot"
	mkdir -p "$BMNT"
	if ! mount -t vfat "${DISK}p3" "$BMNT"; then
		log "cannot mount the new boot partition to fill it; stopping"
		drop_to_shell
	fi
	mkdir -p "$BMNT/extlinux"
	cp "$TREE/Image" "$BMNT/Image" \
		|| { log "copying Image to ${DISK}p3 failed"; drop_to_shell; }
	if [ -f "$TREE/Image.old" ]; then
		cp "$TREE/Image.old" "$BMNT/Image.old" \
			|| { log "copying Image.old to ${DISK}p3 failed"; drop_to_shell; }
	fi
	if [ -f "$TREE/uboot.env" ]; then
		cp "$TREE/uboot.env" "$BMNT/uboot.env" \
			|| { log "copying uboot.env to ${DISK}p3 failed"; drop_to_shell; }
	fi
	cp "$TREE/$dtb" "$BMNT/$dtb" \
		|| { log "copying $dtb to ${DISK}p3 failed"; drop_to_shell; }
	# The eMMC descriptor becomes extlinux.conf: the installed disk boots the
	# primary `t2-emmc` entry, while the card's own descriptor (default
	# `t2-installer`) is only ever read off the card.
	cp "$TREE/extlinux/t2-emmc.conf" "$BMNT/extlinux/extlinux.conf" \
		|| { log "copying the eMMC descriptor failed"; drop_to_shell; }
	got=$(sed -n 's/^default[[:space:]]*//p' "$BMNT/extlinux/extlinux.conf" | head -n 1)
	if [ "$got" != t2-emmc ]; then
		log "the copied extlinux.conf selects '$got', not t2-emmc; refusing"
		drop_to_shell
	fi
	log "boot tree written to ${DISK}p3 (extlinux default t2-emmc)"
	umount "$BMNT" || true
	BMNT=''
	log "writing U-Boot to ${DISK}p1"
	dd if="$TREE/$UBOOT_FILE" of="${DISK}p1" bs=4M conv=fsync 2>/dev/null \
		|| { log "U-Boot write failed"; drop_to_shell; }
	if [ "$IDB" != none ]; then
		log "writing our SPL at LBA 0x40"
		dd if="$TREE/$IDB" of="$DISK" bs=512 seek=64 conv=fsync,notrunc 2>/dev/null \
			|| { log "SPL write failed"; drop_to_shell; }
	else
		log "keeping the vendor SPL already at LBA 0x40"
	fi
	sync
	kill "$blinker" 2>/dev/null || true
	wait "$blinker" 2>/dev/null || true
	blinker=''
	led_set "$LED_RED" 0
	led_set "$LED_GREEN" 1
	log "flash complete; rebooting.  Remove the card now: the card's loader is preferred while it is inserted."
	if [ "${T2_FLASH_NOREBOOT:-}" = 1 ]; then
		log "not rebooting (T2_FLASH_NOREBOOT=1)"
		drop_to_shell
	fi
	reboot_now
}

# ---------------------------------------------------------------------------
# main: find and parse the card's t2-config.txt, then dispatch
# ---------------------------------------------------------------------------
mkdir -p "$TMP"

if [ -n "$CONFIG_DIR" ]; then
	cfgdir=$CONFIG_DIR
else
	dev=''
	for l in $LABELS; do
		dev=$(label_dev "$l" || true)
		[ -n "$dev" ] && break
	done
	if [ -z "$dev" ]; then
		log "no config partition (labels: $LABELS); nothing armed"
		drop_to_shell
	fi
	CFG_MNT="$ROOT/run/t2-installer-config"
	mkdir -p "$CFG_MNT"
	if ! mount -t vfat -o ro "$dev" "$CFG_MNT"; then
		log "cannot mount $dev read-only; nothing armed"
		drop_to_shell
	fi
	cfgdir=$CFG_MNT
fi

cfg="$cfgdir/t2-config.txt"
if [ ! -f "$cfg" ]; then
	log "no t2-config.txt in $cfgdir; nothing armed"
	drop_to_shell
fi

ipresses=''
iwindow=''
ipayload=''
ifile=''
isha=''
itarget=''
ibutton=''
ikey=''
idecomp=''
fpresses=''
fpayload=''
fsha=''
fdisk=''
fwindow=''
fbutton=''
frootfs=''
ftree=''
fuboot=''
fidb=''
fkey=''
fdecompress=''

while IFS= read -r raw || [ -n "$raw" ]; do
	line=$(printf '%s' "$raw" | tr -d '\r')
	case "$line" in
	'' | '#'*) continue ;;
	*=*) ;;
	*)
		log "ignoring malformed line: $line"
		continue
		;;
	esac
	key=${line%%=*}
	val=${line#*=}
	case "$key" in
	install.presses) ipresses=$val ;;
	install.window) iwindow=$val ;;
	install.payload) ipayload=$val ;;
	install.file) ifile=$val ;;
	install.sha256) isha=$val ;;
	install.target) itarget=$val ;;
	install.button) ibutton=$val ;;
	install.key) ikey=$val ;;
	install.decompress) idecomp=$val ;;
	flash.presses) fpresses=$val ;;
	flash.payload) fpayload=$val ;;
	flash.sha256) fsha=$val ;;
	flash.disk) fdisk=$val ;;
	flash.window) fwindow=$val ;;
	flash.button) fbutton=$val ;;
	flash.rootfs) frootfs=$val ;;
	flash.tree) ftree=$val ;;
	flash.uboot) fuboot=$val ;;
	flash.idbloader) fidb=$val ;;
	flash.key) fkey=$val ;;
	flash.decompress) fdecompress=$val ;;
	*) log "ignoring unknown key '$key'" ;;
	esac
done <"$cfg"

install_presses=$ipresses
install_window=$iwindow
install_payload=$ipayload
install_file=$ifile
install_sha256=$isha
install_target=$itarget
install_button=$ibutton
install_key=$ikey
install_decompress=$idecomp
flash_presses=$fpresses
flash_payload=$fpayload
flash_sha256=$fsha
flash_disk=$fdisk
flash_window=$fwindow
flash_button=$fbutton
flash_rootfs=$frootfs
flash_tree=$ftree
flash_uboot=$fuboot
flash_idbloader=$fidb
flash_key=$fkey
flash_decompress=$fdecompress

if [ -n "$ipresses" ] && [ -n "$fpresses" ]; then
	log "both install.presses and flash.presses are set; refusing to guess which flow is wanted"
	drop_to_shell
fi

# Own the power LEDs from here on: the payload verify below can take ~20 s and
# the kernel's green default would otherwise light the whole time.
led_claim

if [ -n "$ipresses" ]; then
	arm_ok "$ipresses" "$iwindow" install || drop_to_shell
	install_run
elif [ -n "$fpresses" ]; then
	arm_ok "$fpresses" "$fwindow" flash || drop_to_shell
	flash_run
else
	log "not armed (no install.presses or flash.presses); nothing to do"
	drop_to_shell
fi
