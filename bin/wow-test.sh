#!/usr/bin/env bash
# bin/wow-test.sh — time the canonical demo from cut to workflow-succeeded.
#
# Usage:
#   bin/wow-test.sh [NODE] [INTERFACE]
# Defaults:
#   NODE=hub-n  INTERFACE=ethernet-1/1
#
# Prints elapsed seconds at three hops:
#   t_cut          → make demo-cut returns
#   t_alert_firing → Prometheus reports SRLInterfaceOperDown firing for the cut interface
#   t_wf_done      → an enrich-notify-* workflow in argo-events reaches Succeeded
#
# Requires an up cluster with monitoring + argo-events deployed.

set -uo pipefail

NODE="${1:-hub-n}"
INTERFACE="${2:-ethernet-1/1}"
TIMEOUT="${WOW_TEST_TIMEOUT:-120}"

prom_pod() {
  kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus \
    -o jsonpath='{.items[0].metadata.name}'
}

prom_query_alerts() {
  local pod=$1
  kubectl -n monitoring exec "$pod" -c prometheus -- \
    wget -qO- http://localhost:9090/api/v1/alerts 2>/dev/null || true
}

restore() {
  echo "==> restoring $NODE/$INTERFACE"
  make -s demo-restore NODE="$NODE" INTERFACE="$INTERFACE" >/dev/null || true
}
trap restore EXIT INT TERM

PROM_POD=$(prom_pod)
if [[ -z "$PROM_POD" ]]; then
  echo "no prometheus pod in monitoring namespace; is the cluster up?" >&2
  exit 1
fi

# Capture pre-cut workflow names so we can detect the new one.
PRE_WFS=$(kubectl -n argo-events get wf -o name 2>/dev/null | sort)

T0=$(date +%s)
echo "==> [$(date +%H:%M:%S)] cut $NODE/$INTERFACE"
make -s demo-cut NODE="$NODE" INTERFACE="$INTERFACE" >/dev/null
T_CUT=$(($(date +%s) - T0))
echo "    t_cut=${T_CUT}s"

# Hop 1: SRLInterfaceOperDown firing for this node/interface.
echo "==> polling Prometheus alerts (timeout ${TIMEOUT}s)"
T_ALERT=""
while (( $(date +%s) - T0 < TIMEOUT )); do
  payload=$(prom_query_alerts "$PROM_POD")
  if echo "$payload" | grep -q "SRLInterfaceOperDown"; then
    if echo "$payload" | NODE="$NODE" INTERFACE="$INTERFACE" python3 -c "
import json, os, sys
node = os.environ['NODE']
interface = os.environ['INTERFACE']
alerts = json.load(sys.stdin).get('data', {}).get('alerts', [])
for a in alerts:
    lbl = a.get('labels', {})
    if (lbl.get('alertname') == 'SRLInterfaceOperDown'
        and lbl.get('node') == node
        and lbl.get('interface') == interface
        and a.get('state') == 'firing'):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
      T_ALERT=$(($(date +%s) - T0))
      echo "    t_alert_firing=${T_ALERT}s"
      break
    fi
  fi
  sleep 1
done
if [[ -z "$T_ALERT" ]]; then
  echo "    timed out waiting for SRLInterfaceOperDown firing" >&2
  exit 2
fi

# Hop 2: new enrich-notify-* workflow Succeeded.
echo "==> polling argo-events workflows for Succeeded"
T_WF=""
while (( $(date +%s) - T0 < TIMEOUT )); do
  CUR_WFS=$(kubectl -n argo-events get wf -o name 2>/dev/null | sort)
  NEW_WFS=$(comm -13 <(echo "$PRE_WFS") <(echo "$CUR_WFS") | grep "^workflow.argoproj.io/enrich-notify-" || true)
  if [[ -n "$NEW_WFS" ]]; then
    while read -r wf; do
      [[ -z "$wf" ]] && continue
      phase=$(kubectl -n argo-events get "$wf" -o jsonpath='{.status.phase}' 2>/dev/null || true)
      if [[ "$phase" == "Succeeded" ]]; then
        T_WF=$(($(date +%s) - T0))
        echo "    t_wf_done=${T_WF}s ($wf)"
        break 2
      fi
    done <<<"$NEW_WFS"
  fi
  sleep 1
done
if [[ -z "$T_WF" ]]; then
  echo "    timed out waiting for enrich-notify-* Succeeded" >&2
  exit 3
fi

echo
echo "==> Summary"
printf "    %-18s %4ss\n" "t_cut"          "$T_CUT"
printf "    %-18s %4ss\n" "t_alert_firing" "$T_ALERT"
printf "    %-18s %4ss\n" "t_wf_done"      "$T_WF"
