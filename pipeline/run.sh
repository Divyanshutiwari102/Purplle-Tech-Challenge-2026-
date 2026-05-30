#!/usr/bin/env bash
# Process every store's clips, emit events JSONL, then POST to the API.
#
# Usage:
#   STORE_INTEL_API=http://localhost:8000 ./pipeline/run.sh data/clips data/layout/store_layout.json
#
set -euo pipefail

CLIPS_DIR="${1:-data/clips}"
LAYOUT="${2:-data/layout/store_layout.json}"
OUT_DIR="${OUT_DIR:-data/events}"
API="${STORE_INTEL_API:-http://localhost:8000}"

mkdir -p "$OUT_DIR"

# Discover unique store IDs from clip filenames (STORE_BLR_002_CAM_*.mp4)
mapfile -t STORE_IDS < <(ls "$CLIPS_DIR" 2>/dev/null \
    | sed -E 's/(STORE_[^_]+_[0-9]+).*/\1/' \
    | sort -u)

if [[ ${#STORE_IDS[@]} -eq 0 ]]; then
    echo "No clips found in $CLIPS_DIR" >&2
    exit 1
fi

for sid in "${STORE_IDS[@]}"; do
    echo ">>> Detecting for $sid"
    python -m pipeline.detect \
        --store-id "$sid" \
        --clips-dir "$CLIPS_DIR" \
        --layout "$LAYOUT" \
        --out-dir "$OUT_DIR"
done

echo ">>> Posting events to $API"
python -m pipeline.replay --events-dir "$OUT_DIR" --api "$API"
