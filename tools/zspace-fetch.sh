#!/usr/bin/env bash
# zspace-fetch.sh - pull a firmware dump off a rooted ZSpace NAS over ssh.
#
# It copies zspace-dump.sh to the device, reads the storage report, then streams
# the interesting block devices/partitions back here compressed, verifying each
# file against a hash the *device* computes over the same bytes. Nothing is
# written on the device except the helper script in /tmp.
#
#   ./zspace-fetch.sh --host root@192.168.1.50
#   ./zspace-fetch.sh --host root@nas --out dumps --all
#   ./zspace-fetch.sh --host root@nas --select 'full:/dev/mmcblk0,full:/dev/mtd0'
#
# Selection defaults to the boot storage (the device holding /): the whole
# device, its GPT head/tail, every partition, and any SPI-NOR MTD partitions.
# NVMe user-data disks are listed but never dumped unless --include-nvme.
#
# --local runs the same flow without ssh, against the fake tree in workbench/,
# which is how this script is tested.

set -uo pipefail

HOST=""
OUT="dumps"
SELECT=""
INCLUDE_NVME=0
ALL=0
VERIFY=1
FORCE=0
LOCAL=0
COMPRESSOR=""
SSH_OPTS=()
DUMPER="$(dirname "$(readlink -f "$0")")/zspace-dump.sh"
REMOTE_SCRIPT="/tmp/zspace-dump.sh"

die() { echo "zspace-fetch: $*" >&2; exit 1; }
log() { echo "zspace-fetch: $*" >&2; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--host) HOST=$2; shift 2 ;;
		--out) OUT=$2; shift 2 ;;
		--select) SELECT=$2; shift 2 ;;
		--all) ALL=1; shift ;;
		--include-nvme) INCLUDE_NVME=1; shift ;;
		--no-verify) VERIFY=0; shift ;;
		--force) FORCE=1; shift ;;
		--local) LOCAL=1; shift ;;
		--ssh-opt) SSH_OPTS+=("$2"); shift 2 ;;
		--compressor) COMPRESSOR=$2; shift 2 ;;
		-h|--help) usage ;;
		*) die "unknown argument: $1" ;;
	esac
done

[ -n "$HOST" ] || [ "$LOCAL" = 1 ] || die "--host is required (or --local for the fixture)"
[ -r "$DUMPER" ] || die "cannot read $DUMPER"
mkdir -p "$OUT"

if [ -z "$COMPRESSOR" ]; then
	if command -v zstd >/dev/null; then COMPRESSOR="zstd -T0 -3 -q"
	elif command -v pigz >/dev/null; then COMPRESSOR="pigz -q"
	elif command -v gzip >/dev/null; then COMPRESSOR="gzip -q"
	else COMPRESSOR="cat"; log "no compressor found; storing raw"
	fi
fi
EXT="zst"
case "$COMPRESSOR" in *pigz*|*gzip*) EXT="gz" ;; cat) EXT="img" ;; esac

# --------------------------------------------------------------- remote access
remote_sh() {   # run a shell command on the device, stdout passes through
	if [ "$LOCAL" = 1 ]; then
		bash -c "$1"
	else
		ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$HOST" "$1"
	fi
}

install_dumper() {
	log "installing dumper to $REMOTE_SCRIPT on the device"
	remote_sh "cat > $REMOTE_SCRIPT && chmod 700 $REMOTE_SCRIPT" < "$DUMPER" ||
		die "failed to copy dumper"
}

# ------------------------------------------------------------------ selection
# plan.txt lines look like:  spec<TAB>description
choose_specs() {
	local plan=$1
	if [ -n "$SELECT" ]; then
		printf '%s\n' "$SELECT" | tr ',' '\n'
		return
	fi
	local rootdev base
	rootdev=$(sed -n 's/^# OS root is on \([^ ]*\).*/\1/p' "$plan" | head -1)
	base=$(basename "${rootdev:-mmcblk0}")
	base=${base%%p[0-9]*}
	base=${base%[0-9]}

	# everything MTD (SPI-NOR / NAND firmware storage) is always interesting
	awk -F'\t' '/^full:.*mtd[0-9]/ {print $1}' "$plan"
	if [ "$ALL" = 1 ]; then
		awk -F'\t' '{print $1}' "$plan" | grep -v '^#' |
			{ if [ "$INCLUDE_NVME" = 1 ]; then cat; else grep -v nvme; fi; }
		return
	fi
	awk -F'\t' -v b="$base" '
		/^full:/ && index($1, "/" b) { print $1; next }
		/^head:/ && index($1, "/" b) { print $1; next }
		/^tail:/ && index($1, "/" b) { print $1; next }
		/^part:/ && index($1, "/" b) { print $1; next }
	' "$plan"
}

# spec -> readable file name
spec_name() {
	local spec=$1 kind rest dev part pname
	kind=${spec%%:*}; rest=${spec#*:}
	dev=${rest%%:*}
	pname=$(basename "$dev")
	case "$kind" in
		part)
			# part:<dev>:<start>:<count> - look up the PARTNAME from the plan
			local name
			name=$(awk -F'\t' -v s="$spec" '$1 == s {print $2}' "$OUT/plan.txt" |
				sed -n 's/^\([^ ]*\) (\([^)]*\)).*/\2/p' | head -1)
			[ -n "$name" ] && pname="${pname}_${name}"
			echo "$pname"
			;;
		head) echo "${pname}_gpt-head" ;;
		tail) echo "${pname}_gpt-tail" ;;
		full) echo "${pname}" ;;
		file) echo "$(basename "$dev")" ;;
		*) echo "${pname}_${kind}" ;;
	esac
}

# ------------------------------------------------------------------- transfer
fetch_one() {
	local spec=$1 name dest
	name=$(spec_name "$spec")
	dest="$OUT/${name}.img.${EXT}"
	if [ -e "$dest" ] && [ "$FORCE" != 1 ]; then
		log "skip $spec (already have $dest; use --force to redo)"
		return 0
	fi
	log "dumping $spec -> $dest"
	if ! remote_sh "sh $REMOTE_SCRIPT dump '$spec'" | $COMPRESSOR > "$dest.tmp"; then
		rm -f "$dest.tmp"
		log "FAILED: $spec"
		return 1
	fi
	mv "$dest.tmp" "$dest"
	log "  wrote $dest ($(du -h "$dest" | cut -f1))"

	if [ "$VERIFY" = 1 ]; then
		local remote_hash local_hash
		remote_hash=$(remote_sh "sh $REMOTE_SCRIPT hash '$spec'" | awk '{print $1}')
		if [ "$EXT" = "zst" ]; then
			local_hash=$(zstd -dc "$dest" | sha256sum | awk '{print $1}')
		elif [ "$EXT" = "gz" ]; then
			local_hash=$(gzip -dc "$dest" | sha256sum | awk '{print $1}')
		else
			local_hash=$(sha256sum "$dest" | awk '{print $1}')
		fi
		if [ -n "$remote_hash" ] && [ "$remote_hash" = "$local_hash" ]; then
			log "  verified sha256 $local_hash"
			echo "$spec $name sha256 $local_hash ok" >> "$OUT/manifest.txt"
		else
			log "  !! HASH MISMATCH for $spec (device $remote_hash, local $local_hash)"
			echo "$spec $name sha256 MISMATCH device=$remote_hash local=$local_hash" \
				>> "$OUT/manifest.txt"
			return 1
		fi
	fi
}

main() {
	install_dumper
	log "collecting system report"
	remote_sh "sh $REMOTE_SCRIPT info" > "$OUT/info.txt" || die "info failed"
	remote_sh "sh $REMOTE_SCRIPT plan" > "$OUT/plan.txt" || die "plan failed"
	sed -n '1,12p' "$OUT/info.txt" >&2
	log "report saved to $OUT/info.txt ($(wc -l < "$OUT/info.txt") lines), plan to $OUT/plan.txt"

	local specs fails=0
	mapfile -t specs < <(choose_specs "$OUT/plan.txt" | sort -u)
	[ "${#specs[@]}" -gt 0 ] || die "no dump targets selected (see $OUT/plan.txt)"
	log "dumping ${#specs[@]} target(s)"
	local spec
	for spec in "${specs[@]}"; do
		[ -n "$spec" ] || continue
		fetch_one "$spec" || fails=$((fails + 1))
	done
	log "done: $(( ${#specs[@]} - fails )) ok, $fails failed"
	case "$EXT" in
		zst) log "next: zstd -d -k $OUT/*.img.zst && python3 $(dirname "$DUMPER")/rk-extract.py --out extracted $OUT/*.img" ;;
		gz)  log "next: gunzip -k $OUT/*.img.gz && python3 $(dirname "$DUMPER")/rk-extract.py --out extracted $OUT/*.img" ;;
		*)   log "next: python3 $(dirname "$DUMPER")/rk-extract.py --out extracted $OUT/*.img" ;;
	esac
	[ "$fails" -eq 0 ]
}

main
