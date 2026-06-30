#!/usr/bin/env bash
# Regenerate a docs GIF: record a Grafana dashboard (headless, Playwright)
# while a scripted fault drives it, then assemble an optimized GIF (ffmpeg).
# Repeatable so the GIFs stay current when dashboards change.
#
# Usage:
#   bin/make-gif.sh                       # geomap reacting to a fiber cut -> docs/assets/grafana-fault.gif
#   DASH=geomap NODE=hub-e INTERFACE=ethernet-1/2 OUT=docs/assets/grafana-fault.gif bin/make-gif.sh
# (DASH, not UID — UID is a reserved shell variable on macOS/Linux.)
#
# Requires: ffmpeg (brew install ffmpeg) and tools/gifgen deps (npm --prefix
# tools/gifgen ci). Cluster must be up (make ready green).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
HERE=$(cd "$(dirname "$0")/.." && pwd); cd "$HERE"

DASH=${DASH:-geomap}
OUT=${OUT:-docs/assets/grafana-fault.gif}
NODE_=${NODE:-hub-e}; IFACE=${INTERFACE:-ethernet-1/2}
FPS=${FPS:-9}; WIDTH=${WIDTH:-1100}
PRE=${PRE:-9}        # seconds of healthy baseline before the cut
RED=${RED:-38}       # seconds to let the dashboard show the fault
GREEN=${GREEN:-31}   # seconds to let it recover after restore

command -v ffmpeg >/dev/null || { echo "ffmpeg not found — brew install ffmpeg" >&2; exit 1; }
[ -d tools/gifgen/node_modules/playwright ] || { echo "playwright not installed — npm --prefix tools/gifgen ci" >&2; exit 1; }

FRAMES=$(mktemp -d)
trap 'rm -rf "$FRAMES"' EXIT
DURATION=$(( PRE + RED + GREEN ))
echo "==> recording dashboard '$DASH' for ${DURATION}s while cutting $NODE_/$IFACE"
( cd tools/gifgen && DURATION=$DURATION node record.mjs "$DASH" "$FRAMES" ) &
REC=$!

sleep "$PRE"
echo "    [$(date +%H:%M:%S)] fiber cut $NODE_/$IFACE"
make -s demo-cut-fiber NODE="$NODE_" INTERFACE="$IFACE" 2>/dev/null || make -s demo-cut NODE="$NODE_" INTERFACE="$IFACE"
sleep "$RED"
echo "    [$(date +%H:%M:%S)] restore"
make -s demo-restore-fiber NODE="$NODE_" INTERFACE="$IFACE" 2>/dev/null || make -s demo-restore NODE="$NODE_" INTERFACE="$IFACE"
wait "$REC"

echo "==> assembling $OUT (ffmpeg, two-pass palette)"
mkdir -p "$(dirname "$OUT")"
ffmpeg -y -loglevel error -framerate "$FPS" -pattern_type glob -i "$FRAMES/frame-*.png" \
  -vf "scale=${WIDTH}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 "$OUT"
echo "==> done: $OUT ($(du -h "$OUT" | cut -f1), $(ls "$FRAMES"/frame-*.png | wc -l | tr -d ' ') frames)"
