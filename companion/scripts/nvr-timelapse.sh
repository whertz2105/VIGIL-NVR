#!/usr/bin/env bash
#
# nvr-timelapse.sh — nightly per-camera timelapse generator for the NVR.
#
# For a given day, gathers each camera's 15-min H.264 segments across all hour
# folders, concatenates them in time order, and time-compresses the full day
# into a ~45s clip. Output: /srv/nvr/timelapses/{YY_MM_DD}/{camera}.mp4
#
# Also prunes timelapse day-folders older than RETENTION_DAYS.
#
# Usage:
#   nvr-timelapse.sh              # process yesterday (UTC)
#   nvr-timelapse.sh 26_06_17     # process a specific day (for testing)
#   nvr-timelapse.sh --prune-only # run only the retention prune
#
# Safe to re-run (idempotent): re-encodes that day's clips, overwriting them.
# Reads recordings read-only; never modifies anything under recordings/.

set -uo pipefail

# ----------------------------- configuration --------------------------------
REC_ROOT="/srv/nvr/recordings"
OUT_ROOT="${NVR_TL_OUT:-/srv/nvr/timelapses}"
LOG_FILE="${NVR_TL_LOG:-/srv/nvr/timelapses/nvr-timelapse.log}"
TZ_NAME="UTC"
TARGET_SECONDS=150         # desired timelapse length (~2.5 min; frames paced to this, capped ≤3 min)
TL_START_HOUR=0            # cover the full 24-hour day: hours [00:00, 24:00)
TL_END_HOUR=24
FRAME_INTERVAL=60          # take one frame per this many seconds of source (1/min)
RETENTION_DAYS=92          # ~3 months; prune day-folders older than this
VAAPI_DEVICE="/dev/dri/renderD128"
# ----------------------------------------------------------------------------

log() {
    # log to stdout and to the logfile
    local msg="[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
    echo "$msg"
    # best-effort file logging; don't die if the dir isn't writable yet
    echo "$msg" >>"$LOG_FILE" 2>/dev/null || true
}

# Convert a YY_MM_DD folder name to YYYY-MM-DD; echoes nothing + returns 1 if
# the name doesn't parse as a valid date.
parse_folder_date() {
    local name="$1"
    if [[ "$name" =~ ^([0-9]{2})_([0-9]{2})_([0-9]{2})$ ]]; then
        local yy="${BASH_REMATCH[1]}" mm="${BASH_REMATCH[2]}" dd="${BASH_REMATCH[3]}"
        local iso="20${yy}-${mm}-${dd}"
        # validate it's a real calendar date
        if date -d "$iso" +%Y-%m-%d >/dev/null 2>&1; then
            echo "$iso"
            return 0
        fi
    fi
    return 1
}

# --------------------------- vaapi capability check -------------------------
# Try a trivial h264_vaapi encode once. If it works, we use the iGPU; otherwise
# fall back to libx264. Result cached in VENC for the run.
detect_encoder() {
    if [[ -e "$VAAPI_DEVICE" ]] && \
       ffmpeg -hide_banner -loglevel error \
            -vaapi_device "$VAAPI_DEVICE" \
            -f lavfi -i testsrc=duration=1:size=320x240:rate=10 \
            -vf 'format=nv12,hwupload' -c:v h264_vaapi \
            -f null - </dev/null >/dev/null 2>&1; then
        echo "vaapi"
    else
        echo "libx264"
    fi
}

# --------------------------- per-camera processing --------------------------
# $1 = day folder (YY_MM_DD), $2 = camera name, $3 = encoder
#
# Sampling is done PER SEGMENT (one frame every FRAME_INTERVAL seconds of each
# 15-min file), NOT by seeking across a concat of the day. The concat demuxer's
# timeline is unreliable with these recordings' timestamps and was producing a
# frozen first-frame "timelapse". Decoding each file alone (clean PTS) and
# `fps=1/FRAME_INTERVAL` gives an accurate one-frame-per-minute sample.
make_timelapse() {
    local day="$1" cam="$2" venc="$3"
    local day_dir="$REC_ROOT/$day"
    local out_dir="$OUT_ROOT/$day"
    local out_final="$out_dir/$cam.mp4"

    local tmpdir
    tmpdir="$(mktemp -d /tmp/nvr-tl-frames.XXXXXX)" || { log "  $cam: mktemp failed"; return 1; }

    # One frame per FRAME_INTERVAL seconds, extracted by fast INPUT-SEEK on each
    # segment (jumps straight to each minute mark — ~4x lighter than decoding the
    # whole file). Frames are numbered globally so they assemble chronologically.
    local count=0 nseg=0 h hh hdir seg dur maxoff off
    for ((h = 10#$TL_START_HOUR; h < 10#$TL_END_HOUR; h++)); do
        hh="$(printf '%02d' "$h")"
        hdir="$day_dir/$hh/$cam"
        [[ -d "$hdir" ]] || continue
        while IFS= read -r seg; do
            [[ -n "$seg" ]] || continue
            dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$seg" 2>/dev/null)"
            if [[ -z "$dur" || "$dur" == "N/A" ]]; then
                log "  $cam: skipping unprobeable segment $(basename "$seg")"
                continue
            fi
            nseg=$((nseg + 1))
            maxoff="$(awk -v d="$dur" 'BEGIN{ printf "%d", d }')"
            for ((off = 0; off < maxoff; off += FRAME_INTERVAL)); do
                ffmpeg -hide_banner -loglevel error -nostdin \
                    -ss "$off" -i "$seg" -frames:v 1 -q:v 3 \
                    "$tmpdir/$(printf 'f_%07d' "$count").jpg" 2>>"$LOG_FILE" || true
                count=$((count + 1))
            done
        done < <(find "$hdir" -maxdepth 1 -type f -name '*.mp4' 2>/dev/null | LC_ALL=C sort)
    done

    # Count what actually got written (failed seeks leave gaps; glob handles them).
    local nframes
    nframes="$(find "$tmpdir" -name 'f_*.jpg' | wc -l)"
    if [[ "$nframes" -eq 0 ]]; then
        log "  $cam: no frames for $day — skipping camera"
        rm -rf "$tmpdir"
        return 0
    fi

    # Pace the per-minute frames so the clip lands near TARGET_SECONDS long.
    local out_fps
    out_fps="$(awk -v n="$nframes" -v t="$TARGET_SECONDS" \
        'BEGIN{ f=n/t; if(f<1)f=1; if(f>30)f=30; printf "%.4f", f }')"

    mkdir -p "$out_dir"
    local out_tmp="$out_dir/.$cam.tmp.mp4"

    log "  $cam: $nframes frames (1 per ${FRAME_INTERVAL}s, ${TL_START_HOUR}:00–${TL_END_HOUR}:00, $nseg segments) → ${out_fps}fps, ~${TARGET_SECONDS}s"

    # Downscale to 720p + CRF 28: every minute-apart frame differs a lot (poor
    # inter-frame compression), so full-res near-lossless balloons to ~250MB/day.
    # 720p/crf28 keeps it watchable at ~30-40MB/day.
    local -a enc_args=()
    if [[ "$venc" == "vaapi" ]]; then
        enc_args=( -vaapi_device "$VAAPI_DEVICE" -vf 'scale=1280:-2,format=nv12,hwupload' -c:v h264_vaapi -qp 26 )
    else
        enc_args=( -vf 'scale=1280:-2' -c:v libx264 -preset slow -crf 28 -pix_fmt yuv420p )
    fi

    if ffmpeg -hide_banner -loglevel error -y -threads 2 \
        -framerate "$out_fps" -pattern_type glob -i "$tmpdir/f_*.jpg" \
        "${enc_args[@]}" -movflags +faststart -an \
        "$out_tmp" 2>>"$LOG_FILE"; then
        mv -f "$out_tmp" "$out_final"
        local odur
        odur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out_final" 2>/dev/null)"
        log "  $cam: wrote $out_final (${odur}s, $(du -h "$out_final" | cut -f1))"
    else
        log "  $cam: ffmpeg assemble FAILED for $day (see log); leaving previous output"
        rm -f "$out_tmp"; rm -rf "$tmpdir"
        return 1
    fi

    rm -rf "$tmpdir"
    return 0
}

# ------------------------------- prune step ---------------------------------
prune_old() {
    log "Prune: removing timelapse day-folders older than ${RETENTION_DAYS} days"
    [[ -d "$OUT_ROOT" ]] || { log "Prune: no output root yet, nothing to do"; return 0; }

    local cutoff_epoch now_epoch
    now_epoch="$(date +%s)"
    cutoff_epoch=$(( now_epoch - RETENTION_DAYS*86400 ))

    local removed=0 kept=0
    for d in "$OUT_ROOT"/*/; do
        [[ -d "$d" ]] || continue
        local name iso folder_epoch
        name="$(basename "$d")"
        if ! iso="$(parse_folder_date "$name")"; then
            # Not a YY_MM_DD folder (e.g. a stray file/dir) — leave it alone.
            continue
        fi
        folder_epoch="$(date -d "$iso" +%s 2>/dev/null)" || continue
        if (( folder_epoch < cutoff_epoch )); then
            rm -rf "$d"
            log "  pruned $name ($iso, older than ${RETENTION_DAYS}d)"
            removed=$((removed+1))
        else
            kept=$((kept+1))
        fi
    done
    log "Prune: removed $removed, kept $kept"
}

# --------------------------------- main -------------------------------------
main() {
    mkdir -p "$OUT_ROOT" 2>/dev/null || true

    local day=""
    local prune_only=0
    case "${1:-}" in
        --prune-only) prune_only=1 ;;
        "" ) day="$(TZ="$TZ_NAME" date -d 'yesterday' +%y_%m_%d)" ;;
        * )
            if parse_folder_date "$1" >/dev/null; then
                day="$1"
            else
                log "ERROR: '$1' is not a valid YY_MM_DD date or known flag"
                exit 2
            fi
            ;;
    esac

    log "=== nvr-timelapse start (pid $$) ==="

    if [[ "$prune_only" -eq 1 ]]; then
        prune_old
        log "=== nvr-timelapse done (prune-only) ==="
        return 0
    fi

    local day_dir="$REC_ROOT/$day"
    if [[ ! -d "$day_dir" ]]; then
        log "No recordings folder for $day ($day_dir) — nothing to generate"
        prune_old
        log "=== nvr-timelapse done ==="
        return 0
    fi

    local venc
    venc="$(detect_encoder)"
    log "Day $day: using encoder '$venc'"

    # Enumerate cameras dynamically: any subdir name that appears under any hour
    # folder of this day. (Filename sort / dedup.)
    mapfile -t cameras < <(
        find "$day_dir" -mindepth 2 -maxdepth 2 -type d -printf '%f\n' 2>/dev/null \
            | LC_ALL=C sort -u
    )

    if [[ "${#cameras[@]}" -eq 0 ]]; then
        log "Day $day: no camera folders found — nothing to do"
    else
        log "Day $day: cameras found: ${cameras[*]}"
        for cam in "${cameras[@]}"; do
            make_timelapse "$day" "$cam" "$venc"
        done
    fi

    prune_old
    log "=== nvr-timelapse done ==="
}

main "$@"
