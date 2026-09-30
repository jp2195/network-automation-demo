#!/usr/bin/env bash
# bin/wait-ready.sh — poll bin/ready.sh until the lab is functionally ready.
#
# A fresh `make up` takes ~10-20 min to converge (charts sync, SR Linux boots,
# the 300s SNMP poll lands). This loops the readiness gate every INTERVAL
# seconds for up to TIMEOUT seconds, printing one progress line per attempt.
#
# Usage:  bin/wait-ready.sh          (or: make wait-ready)
# Env:    TIMEOUT (default 1500 = 25 min), INTERVAL (default 20)
# Exit:   0 = READY; 1 = timed out (prints the last full gate output).

set -uo pipefail

TIMEOUT="${TIMEOUT:-1500}"
INTERVAL="${INTERVAL:-20}"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ESC=$(printf '\033')

start=$SECONDS
echo "==> Waiting for the lab to be demo-ready (timeout $((TIMEOUT / 60))m, checking every ${INTERVAL}s)"
while :; do
  out=$("$HERE/ready.sh" 2>&1)
  rc=$?
  elapsed=$((SECONDS - start))
  stamp=$(printf '%02d:%02d' $((elapsed / 60)) $((elapsed % 60)))
  if [ "$rc" -eq 0 ]; then
    printf '%s\n' "$out"
    echo "==> READY after $stamp"
    exit 0
  fi
  # Concise progress: just the names of the failing checks (text before ':').
  failing=$(printf '%s\n' "$out" | sed "s/${ESC}\[[0-9;]*m//g" \
    | awk '/✗/{sub(/^.*✗[ ]*/,""); sub(/:.*/,""); printf "%s%s", sep, $0; sep=", "}')
  echo "  [$stamp] not ready — waiting on: ${failing:-readiness gate}"
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo
    printf '%s\n' "$out"
    echo "==> Timed out after $stamp. See docs/runbook-troubleshoot.md; 'make status' shows ArgoCD app state."
    exit 1
  fi
  sleep "$INTERVAL"
done
