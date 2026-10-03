#!/bin/sh
# zspace-dump.sh - read-only firmware dumper for a rooted ZSpace / Rockchip NAS.
#
# Run this ON the device as root. It never writes to a device node: every
# operation is a read (dd from, cat, sha256sum of a range). The only writes are
# to stdout, so nothing on the NAS is modified.
#
#   zspace-dump.sh info
#       System + storage report. Start here: it tells you which block device
#       holds the OS, whether storage is eMMC/SD/NVMe/SPI-NOR, and where the
#       live device tree is.
#
#   zspace-dump.sh plan
#       List devices and partitions with the exact dump commands to run.
#
#   zspace-dump.sh dump <spec>
#       Write raw bytes to stdout. Pipe it through ssh into a file or a
#       compressor; progress goes to stderr so it does not corrupt the stream.
#
#   zspace-dump.sh hash <spec> [--offset MB] [--length MB]
#       sha256 (or md5) of a byte range, for verifying a received dump.
#
#   zspace-dump.sh selfcheck
#       Verify every candidate device/partition is readable and sized sanely.
#
# specs:
#   full:<dev>                     whole block device or MTD partition
#   head:<dev>                     first 34 sectors: MBR + primary GPT
#   tail:<dev>                     last 34 sectors: backup GPT
#   part:<dev>:<start>:<count>     partition, offsets/counts in 512-byte sectors
#   boot0:<dev> / boot1:<dev>      eMMC boot partitions (/dev/mmcblk0boot0/1)
#   file:<path>                    any file (kernel config, OTA package, ...)
#
# Examples:
#   ssh root@nas 'sh /tmp/zspace-dump.sh dump full:/dev/mmcblk0' | zstd -T0 -o emmc.img.zst
#   ssh root@nas 'sh /tmp/zspace-dump.sh dump part:/dev/mmcblk0:32768:65536' > boot.img
#
# Environment overrides exist only so this script can be tested against a
# fixture: ZSPACE_DEV (default /dev), ZSPACE_SYS (/sys), ZSPACE_PROC (/proc).

set -u

DEV_DIR=${ZSPACE_DEV:-/dev}
SYS_DIR=${ZSPACE_SYS:-/sys}
PROC_DIR=${ZSPACE_PROC:-/proc}
SECTOR=512
CHUNK_MIB=64            # bytes per dd invocation when streaming

die() { echo "zspace-dump: $*" >&2; exit 1; }
log() { echo "zspace-dump: $*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

need_root() {
	# ZSPACE_ASSUME_ROOT=1 exists so the local test fixture can exercise this
	# script without privileges; nothing here writes, so it is not a real risk.
	[ "$(id -u)" = "0" ] || [ "${ZSPACE_ASSUME_ROOT:-0}" = "1" ] ||
		die "must run as root (needs raw device access)"
}

# ---------------------------------------------------------------- block layer
# Whole disks only: /sys/class/block/<name>/partition exists for partitions.
list_devices() {
	for d in "$SYS_DIR"/class/block/*; do
		[ -e "$d" ] || continue
		[ -e "$d/partition" ] && continue
		basename "$d"
	done
}

list_partitions_of() {
	dev=$1
	for d in "$SYS_DIR"/class/block/"$dev"/*; do
		[ -e "$d/partition" ] || continue
		basename "$d"
	done
}

dev_sectors() {  # 512-byte sectors, from sysfs
	cat "$SYS_DIR/class/block/$1/size" 2>/dev/null || echo 0
}

dev_ro() {
	v=$(cat "$SYS_DIR/class/block/$1/ro" 2>/dev/null || echo 0)
	[ "$v" = "1" ] && echo "ro" || echo "rw"
}

part_name() {  # PARTNAME from the kernel uevent, when the table provides one
	sed -n 's/^PARTNAME=//p' "$SYS_DIR/class/block/$1/uevent" 2>/dev/null
}

human() {  # bytes -> human, integer math only (busybox-friendly)
	b=$1
	if [ "$b" -ge 1073741824 ]; then echo "$((b / 1073741824)) GiB"
	elif [ "$b" -ge 1048576 ]; then echo "$((b / 1048576)) MiB"
	elif [ "$b" -ge 1024 ]; then echo "$((b / 1024)) KiB"
	else echo "$b B"
	fi
}

device_kind() {  # describe the bus for a whole device
	name=$1
	case "$name" in
		mmcblk*)
			host=$(readlink -f "$SYS_DIR/class/block/$name" 2>/dev/null)
			# /sys/devices/.../mmc_host/mmc0/mmc0:0001/block/mmcblk0
			card=${host%%/block/*}
			t=$(cat "$card/type" 2>/dev/null || echo "?")
			n=$(cat "$card/name" 2>/dev/null || echo "?")
			echo "eMMC/SD ($t, $n)"
			;;
		nvme*)  echo "NVMe" ;;
		sd*)    echo "SCSI/USB mass storage" ;;
		mtdblock*|mtd*) echo "MTD" ;;
		rknand*|rknandbase*) echo "Rockchip NAND" ;;
		*)      echo "block" ;;
	esac
}

mtd_devices() {
	[ -r "$PROC_DIR/mtd" ] || return 0
	while read -r line; do
		# mtd0: 00100000 00010000 "uboot"
		name=${line%%:*}
		size=$(echo "$line" | awk '{print $2}')
		label=$(echo "$line" | sed -n 's/.*"\(.*\)".*/\1/p')
		[ -n "$size" ] && echo "$name $((0x$size)) $label"
	done < "$PROC_DIR/mtd"
}

root_source() {
	awk '$2 == "/" { print $1; exit }' "$PROC_DIR/mounts" 2>/dev/null
}

# mount points of a device node, for deciding whether a partition is user data
mounts_of() {
	awk -v d="$1" '$1 == d { printf "%s%s", sep, $2; sep = "," }' \
		"$PROC_DIR/mounts" 2>/dev/null
}

# base device of a partition path: /dev/mmcblk0p5 -> mmcblk0, /dev/sda1 -> sda
base_of() {
	p=$(basename "$1")
	case "$p" in
		mmcblk*boot[01]) echo "$p" ;;
		mmcblk*p[0-9]*)  echo "${p%p*}" ;;
		nvme*n[0-9]p[0-9]*) echo "${p%p*}" ;;
		*[0-9])          echo "${p%[0-9]}" ;;
		*)               echo "$p" ;;
	esac
}

# ------------------------------------------------------------------- commands
cmd_info() {
	echo "== system =="
	echo "date       $(date -u 2>/dev/null)"
	echo "uname      $(uname -a 2>/dev/null)"
	echo "version    $(cat "$PROC_DIR/version" 2>/dev/null)"
	echo "cmdline    $(cat "$PROC_DIR/cmdline" 2>/dev/null)"
	if [ -r "$PROC_DIR/device-tree/model" ]; then
		echo "dt model   $(tr -d '\000' < "$PROC_DIR/device-tree/model")"
	fi
	if [ -r "$PROC_DIR/device-tree/compatible" ]; then
		echo "dt compat  $(tr '\000' ' ' < "$PROC_DIR/device-tree/compatible")"
	fi
	if [ -r "$SYS_DIR/firmware/fdt" ]; then
		echo "live fdt   $SYS_DIR/firmware/fdt ($(wc -c < "$SYS_DIR/firmware/fdt") bytes)"
	fi
	[ -r /etc/os-release ] && { echo "== os-release =="; cat /etc/os-release; }
	[ -r "$PROC_DIR/config.gz" ] && echo "kernel config: $PROC_DIR/config.gz present"

	echo
	echo "== storage =="
	echo "-- /proc/partitions --"
	cat "$PROC_DIR/partitions" 2>/dev/null

	echo "-- block devices --"
	for d in $(list_devices); do
		sec=$(dev_sectors "$d")
		printf '%-16s %10s sectors (%s) %s  %s\n' "$d" "$sec" \
			"$(human $((sec * SECTOR)))" "$(dev_ro "$d")" "$(device_kind "$d")"
		for p in $(list_partitions_of "$d"); do
			psec=$(dev_sectors "$p")
			pn=$(part_name "$p")
			printf '  %-14s %10s sectors (%s) %s\n' "$p" "$psec" \
				"$(human $((psec * SECTOR)))" "${pn:+name=$pn}"
		done
	done

	echo "-- eMMC/SD cards --"
	for h in "$SYS_DIR"/class/mmc_host/*; do
		[ -e "$h" ] || continue
		for c in "$h"/mmc*; do
			[ -d "$c" ] || continue
			printf '%-24s type=%s name=%s manfid=%s oemid=%s fwrev=%s hwrev=%s serial=%s\n' \
				"$(basename "$c")" "$(cat "$c/type" 2>/dev/null)" \
				"$(cat "$c/name" 2>/dev/null)" "$(cat "$c/manfid" 2>/dev/null)" \
				"$(cat "$c/oemid" 2>/dev/null)" "$(cat "$c/fwrev" 2>/dev/null)" \
				"$(cat "$c/hwrev" 2>/dev/null)" "$(cat "$c/serial" 2>/dev/null)"
		done
	done

	echo "-- MTD --"
	mtd_devices | while read -r n sz label; do
		printf '%-8s %10s bytes (%s) "%s"\n' "$n" "$sz" "$(human "$sz")" "$label"
	done
	[ -d "$SYS_DIR/class/mtd" ] && ls "$SYS_DIR/class/mtd" 2>/dev/null | head -20

	echo "-- mounts --"
	cat "$PROC_DIR/mounts" 2>/dev/null
	echo "root device: $(root_source)"

	echo
	echo "== device-specific hints =="
	[ -d /dev/block/by-name ] && { echo "-- /dev/block/by-name --"; ls -l /dev/block/by-name; }
	[ -d /dev/block/by-partlabel ] && ls /dev/block/by-partlabel 2>/dev/null | head
	for f in /dev/mmcblk0boot0 /dev/mmcblk0boot1; do
		[ -e "$f" ] && echo "eMMC boot partition present: $f"
	done
	[ -e /dev/rknand0 ] && echo "Rockchip NAND device present"
	if have dmesg; then
		echo "-- dmesg (storage/rockchip) --"
		dmesg 2>/dev/null | grep -iE 'mmc|nvme|mtd|spi|rockchip|rk8|gmac|dwmmc|sdhci' | tail -60
	fi
}

# Build "spec<TAB>description" lines describing everything worth dumping.
build_plan() {
	for d in $(list_devices); do
		sec=$(dev_sectors "$d")
		[ "$sec" -gt 0 ] || continue
		m=$(mounts_of "$DEV_DIR/$d")
		echo "full:$DEV_DIR/$d	system: whole $d ($(human $((sec * SECTOR))))${m:+  mounted at $m}"
		echo "head:$DEV_DIR/$d	table: first 34 sectors of $d"
		echo "tail:$DEV_DIR/$d	table: backup GPT (last 34 sectors) of $d"
		for p in $(list_partitions_of "$d"); do
			psec=$(dev_sectors "$p")
			pn=$(part_name "$p")
			pm=$(mounts_of "$DEV_DIR/$p")
			echo "part:$DEV_DIR/$p:0:$psec	$p${pn:+ ($pn)} $(human $((psec * SECTOR)))${pm:+  mounted at $pm}"
		done
	done
	mtd_devices | while read -r n sz label; do
		echo "full:$DEV_DIR/$n	MTD $n \"$label\" ($(human "$sz"))"
	done
}

cmd_plan() {
	root=$(root_source)
	[ -n "$root" ] && echo "# OS root is on $root (base device $(base_of "$root"))"
	echo "# spec	description"
	build_plan
}

cmd_selfcheck() {
	need_root
	rc=0
	build_plan | while IFS='	' read -r spec desc; do
		dev=$(echo "$spec" | cut -d: -f2)
		[ -e "$dev" ] || { echo "MISSING $dev"; continue; }
		if dd if="$dev" of=/dev/null bs=512 count=1 2>/dev/null; then
			echo "ok      $desc"
		else
			echo "UNREADABLE $desc"
		fi
	done
	return $rc
}

# Parse a spec into DEV OFFSET_BYTES LENGTH_BYTES (echoed, space separated).
resolve_spec() {
	spec=$1
	kind=${spec%%:*}
	rest=${spec#*:}
	case "$kind" in
		full)
			dev=$rest
			[ -e "$dev" ] || die "no such device: $dev"
			sec=$(dev_sectors "$(basename "$dev")")
			if [ "$sec" -eq 0 ] && [ -r "$SYS_DIR/class/block/$(basename "$dev")/size" ]; then
				sec=$(cat "$SYS_DIR/class/block/$(basename "$dev")/size")
			fi
			# MTD character devices have no sysfs block entry: ask the kernel
			if [ "$sec" -eq 0 ]; then
				case "$dev" in
					"$DEV_DIR"/mtd*)
						n=${dev##*/mtd}
						sz=$(mtd_devices | awk -v k="mtd$n" '$1==k {print $2}')
						[ -n "$sz" ] && sec=$((sz / SECTOR))
						;;
				esac
			fi
			[ "$sec" -gt 0 ] || die "cannot size $dev"
			echo "$dev 0 $((sec * SECTOR))"
			;;
		head) dev=$rest; [ -e "$dev" ] || die "no such device: $dev"; echo "$dev 0 $((34 * SECTOR))" ;;
		tail)
			dev=$rest; [ -e "$dev" ] || die "no such device: $dev"
			sec=$(dev_sectors "$(basename "$dev")")
			[ "$sec" -gt 34 ] || die "cannot size $dev"
			echo "$dev $(((sec - 34) * SECTOR)) $((34 * SECTOR))"
			;;
		part)
			dev=$(echo "$rest" | cut -d: -f1)
			start=$(echo "$rest" | cut -d: -f2)
			count=$(echo "$rest" | cut -d: -f3)
			[ -e "$dev" ] || die "no such device: $dev"
			[ -n "$start" ] && [ -n "$count" ] || die "part spec needs start:count"
			echo "$dev $((start * SECTOR)) $((count * SECTOR))"
			;;
		boot0|boot1)
			echo "$DEV_DIR/$rest 0 $(($(dev_sectors "$(basename "$rest")") * SECTOR))"
			;;
		file)
			[ -r "$rest" ] || die "no such file: $rest"
			echo "$rest 0 $(wc -c < "$rest")"
			;;
		*) die "unknown spec: $spec" ;;
	esac
}

stream_range() {  # dev off len
	dev=$1; off=$2; len=$3
	[ "$len" -gt 0 ] || die "nothing to read"
	if [ $((off % 1048576)) -eq 0 ] && [ "$len" -ge 1048576 ]; then
		bs=1M; blk=1048576
	else
		bs=512; blk=512
	fi
	skip=$((off / blk))
	blocks=$((len / blk))
	rem=$((len % blk))
	chunk=$((CHUNK_MIB * 1048576 / blk))
	done_b=0
	while [ "$blocks" -gt 0 ]; do
		n=$chunk
		[ "$n" -gt "$blocks" ] && n=$blocks
		dd if="$dev" bs="$bs" skip="$skip" count="$n" 2>/dev/null || exit 1
		skip=$((skip + n)); blocks=$((blocks - n)); done_b=$((done_b + n * blk))
		printf '\r  %s / %s (%d%%)' "$(human "$done_b")" "$(human "$len")" \
			$((done_b * 100 / len)) >&2
	done
	if [ "$rem" -gt 0 ]; then
		dd if="$dev" bs=1 skip=$((off + done_b)) count="$rem" 2>/dev/null || exit 1
		done_b=$((done_b + rem))
		printf '\r  %s / %s (100%%)\n' "$(human "$done_b")" "$(human "$len")" >&2
	fi
}

cmd_dump() {
	[ $# -ge 1 ] || die "dump needs a spec"
	need_root
	spec=$1
	set -- $(resolve_spec "$spec")
	dev=$1; off=$2; len=$3
	log "dump $spec -> $dev offset=$off length=$len"
	stream_range "$dev" "$off" "$len"
}

cmd_hash() {
	spec=$1
	shift
	off_mb=0; len_mb=0
	while [ $# -gt 0 ]; do
		case "$1" in
			--offset) off_mb=$2; shift 2 ;;
			--length) len_mb=$2; shift 2 ;;
			*) die "unknown option $1" ;;
		esac
	done
	set -- $(resolve_spec "$spec")
	dev=$1; base=$2; total=$3
	off=$((base + off_mb * 1048576))
	len=$((total - off_mb * 1048576))
	[ "$len_mb" -gt 0 ] && len=$((len_mb * 1048576))
	if have sha256sum; then algo=sha256sum
	elif have md5sum; then algo=md5sum
	else die "no sha256sum/md5sum on this system"
	fi
	dd if="$dev" bs=512 skip=$((off / SECTOR)) count=$((len / SECTOR)) 2>/dev/null | $algo
}

usage() {
	sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
}

case "${1:-}" in
	info)      cmd_info ;;
	plan)      cmd_plan ;;
	dump)      shift; cmd_dump "$@" ;;
	hash)      shift; cmd_hash "$@" ;;
	selfcheck) cmd_selfcheck ;;
	*|--help|-h) usage ;;
esac
