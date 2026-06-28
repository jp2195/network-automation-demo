#!/usr/bin/env bash
# bin/measure.sh — multi-run latency harness for the paper's results table.
#
# Runs N independent cut → detect → enriched-notify cycles on one telemetry
# lane, captures EXACT cluster-object timestamps (not host poll deltas), and
# emits a CSV plus a summary (mean / median / p95 / min / max) per metric.
#
# Usage:
#   bin/measure.sh [-n N] [-l gnmi|snmp] [-I "node:if node:if ..."] [-N NODE -i INTERFACE] [-o OUT.csv]
#
# Interface selection (run i uses the i-th entry, cycling if the list is short):
#   default gnmi:  the SRL-side links from workloads/observability/link-membership.yaml
#                  (26 distinct: SRLInterfaceOperDown, for:0s)
#   default snmp:  fc-n / fc-nw / fc-i20e / fc-sw : eth1
#                  (4 cabinets: CabinetInterfaceOperDown, for:1m)
#   -I "..."       supply your own rotation list
#   -N/-i together pin a single fixed interface (no rotation)
#
# Rotating distinct interfaces keeps each run independent (the incident ledger
# is keyed by alert fingerprint) and guards against single-port cherry-picking;
# detection latency is interface-invariant here, so the jitter sampled is the
# same either way. See the rotation-list section below.
#
# Metrics captured per run (seconds, relative to the cut being issued):
#   detect_s   alert activeAt - t0    telemetry + scrape + rule-eval ONLY
#                                     (excludes the alert `for:` debounce)
#   fire_s     detect_s + for_s       alert becomes actionable (incl. debounce)
#   notify_s   wf finishedAt - t0     end-to-end: cut → enriched notification ready
#   enrich_s   notify_s - fire_s      enrich+analyze+notify pipeline (lane-independent)
#   wf_dur_s   wf finished - started  pure Argo workflow execution time
#
# CLOCK MODEL: t0 is the host wall clock at cut-issue; activeAt / finishedAt are
# Prometheus / Argo object timestamps. Under k3d/kind every container shares the
# host kernel clock, so these are directly comparable. Report the environment
# (single-host k3d on Apple Silicon) alongside the numbers — the defensible
# claim is the gNMI-vs-SNMP delta, not the absolute milliseconds.
#
# Requires an up cluster with monitoring + argo-events deployed.

set -uo pipefail

N=10
LANE=gnmi
NODE=""
INTERFACE=""
IFACES=""        # explicit "node:iface node:iface ..." rotation list (overrides defaults)
OUT=""
TIMEOUT="${MEASURE_TIMEOUT:-180}"        # per-hop wait ceiling
SETTLE_BUFFER="${MEASURE_SETTLE:-10}"    # quiet seconds after a run resolves

while getopts ":n:l:N:i:I:o:h" opt; do
  case "$opt" in
    n) N=$OPTARG ;;
    l) LANE=$OPTARG ;;
    N) NODE=$OPTARG ;;
    i) INTERFACE=$OPTARG ;;
    I) IFACES=$OPTARG ;;
    o) OUT=$OPTARG ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag -$OPTARG (try -h)" >&2; exit 64 ;;
  esac
done

case "$LANE" in
  gnmi)
    ALERTNAME=SRLInterfaceOperDown
    FOR_S=0
    MATCH_INTERFACE=1
    CUT()     { make -s demo-cut         NODE="$NODE" INTERFACE="$INTERFACE" >/dev/null; }
    RESTORE() { make -s demo-restore     NODE="$NODE" INTERFACE="$INTERFACE" >/dev/null 2>&1 || true; }
    ;;
  snmp)
    ALERTNAME=CabinetInterfaceOperDown
    FOR_S=60
    MATCH_INTERFACE=0   # SNMP alert labels carry node+ifIndex, not an interface name
    # Realistic carrier loss (cable pull): down the POD-SIDE veth (<node>-<iface>,
    # e.g. fc-n-eth1) so the cabinet interface goes oper-down while admin stays up
    # — the exact ifOperStatus==2 AND ifAdminStatus==1 the alert requires. vtysh
    # `shutdown` would trip admin-down too, which the alert deliberately excludes,
    # so it never fires. The carrier loss does NOT propagate back across the VXLAN
    # to the SR Linux side, so only CabinetInterfaceOperDown fires (no gNMI-lane
    # contamination of the notify timing).
    _cabpod() { kubectl -n clabernetes get pod -l clabernetes/topologyNode="$NODE" \
                  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
    CUT()     { kubectl -n clabernetes exec "$(_cabpod)" -- ip link set "$NODE-$INTERFACE" down >/dev/null 2>&1; }
    RESTORE() { kubectl -n clabernetes exec "$(_cabpod)" -- ip link set "$NODE-$INTERFACE" up   >/dev/null 2>&1 || true; }
    ;;
  *) echo "lane must be gnmi or snmp (got '$LANE')" >&2; exit 64 ;;
esac

# --- Build the rotation list of node:interface stimuli ---------------------
# Distinct interfaces per run keep each cycle independent: the incident ledger
# is keyed by alert fingerprint (a hash of labels), so repeating one interface
# reuses one fingerprint and couples a run's resolve path to the next run's
# firing. Distinct interfaces => distinct fingerprints => independent trials.
# Detection latency is interface-invariant here (every link shares the same
# 5s sample / 15s scrape / 30s eval / `for:`), so rotating samples the SAME
# jitter distribution while also defending against "you cherry-picked a port".
LMEMB="${LINK_MEMBERSHIP:-workloads/observability/link-membership.yaml}"

default_gnmi_ifaces() {  # SRL-side links only (exclude fc-* FRR cabinets)
  python3 - "$LMEMB" <<'PY'
import re, sys
try:
    txt = open(sys.argv[1]).read()
except OSError:
    txt = ""
pairs = list(zip(re.findall(r'node:\s*"([^"]+)"', txt),
                 re.findall(r'interface:\s*"([^"]+)"', txt)))
seen, out = set(), []
for n, i in pairs:
    if n.startswith("fc-"):      # FRR cabinets have no gNMI/SRL metrics
        continue
    if (n, i) not in seen:
        seen.add((n, i)); out.append(f"{n}:{i}")
# Fallback to the backbone ring if the rendered file wasn't found.
print(" ".join(out) or
      "hub-n:ethernet-1/1 hub-e:ethernet-1/2 hub-e:ethernet-1/1 "
      "hub-i20e:ethernet-1/2 hub-i20e:ethernet-1/1 hub-sw:ethernet-1/2 "
      "hub-sw:ethernet-1/1 hub-i20w:ethernet-1/2 hub-i20w:ethernet-1/1 "
      "hub-nw:ethernet-1/2 hub-nw:ethernet-1/1 hub-n:ethernet-1/2")
PY
}

if [[ -n "$NODE" && -n "$INTERFACE" ]]; then
  IFACE_LIST=("$NODE:$INTERFACE")          # explicit -N/-i pins a single interface
elif [[ -n "$IFACES" ]]; then
  read -r -a IFACE_LIST <<<"$IFACES"        # caller-supplied rotation list
elif [[ "$LANE" == "snmp" ]]; then
  IFACE_LIST=(fc-n:eth1 fc-nw:eth1 fc-i20e:eth1 fc-sw:eth1)  # only 4 cabinets exist
else
  read -r -a IFACE_LIST <<<"$(default_gnmi_ifaces)"
fi
NDISTINCT=$(printf '%s\n' "${IFACE_LIST[@]}" | sort -u | wc -l | tr -d ' ')

: "${OUT:=results/measure-${LANE}-${N}runs.csv}"
mkdir -p "$(dirname "$OUT")"

prom_pod() {
  kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}
prom_alerts() {
  kubectl -n monitoring exec "$PROM_POD" -c prometheus -- \
    wget -qO- http://localhost:9090/api/v1/alerts 2>/dev/null || true
}

# Print "activeAt<TAB>state" for the matching alert, or nothing.
match_alert() {
  ALERTNAME="$ALERTNAME" NODE="$NODE" INTERFACE="$INTERFACE" \
  MATCH_INTERFACE="$MATCH_INTERFACE" python3 -c '
import json, os, sys
want_iface = os.environ["MATCH_INTERFACE"] == "1"
an, node, iface = os.environ["ALERTNAME"], os.environ["NODE"], os.environ["INTERFACE"]
try:
    alerts = json.load(sys.stdin).get("data", {}).get("alerts", [])
except Exception:
    sys.exit(0)
for a in alerts:
    l = a.get("labels", {})
    if l.get("alertname") != an or l.get("node") != node:
        continue
    if want_iface and l.get("interface") != iface:
        continue
    print(a.get("activeAt", "") + "\t" + a.get("state", ""))
    break
'
}

iso2epoch() {  # ISO8601 (with optional frac seconds / Z) -> epoch seconds (float)
  python3 -c '
import sys, datetime
s = sys.argv[1].strip().replace("Z", "+00:00")
if "." in s:                       # clamp fractional seconds to 6 digits
    head, rest = s.split(".", 1)
    frac = "".join(c for c in rest if c.isdigit())[:6]
    tz = rest[len(frac):] if not rest[len(frac):].isdigit() else ""
    # recover any timezone suffix that followed the fractional part
    for i,c in enumerate(rest):
        if c in "+-" and i>0:
            tz = rest[i:]; break
    s = f"{head}.{frac}{tz}"
print(datetime.datetime.fromisoformat(s).timestamp())
' "$1" 2>/dev/null
}

echo "==> lane=$LANE runs=$N for=${FOR_S}s -> $OUT"
echo "==> rotating ${#IFACE_LIST[@]} interfaces ($NDISTINCT distinct): ${IFACE_LIST[*]}"
if (( NDISTINCT < N )); then
  echo "    note: $NDISTINCT distinct interfaces for $N runs — some fingerprints repeat;" \
       "resolve+settle between runs keeps them independent" >&2
fi
PROM_POD=$(prom_pod)
if [[ -z "$PROM_POD" ]]; then
  echo "no prometheus pod in monitoring namespace; is the cluster up?" >&2
  exit 1
fi

cleanup() { echo "==> cleanup restore $NODE/$INTERFACE"; RESTORE; }
trap cleanup EXIT INT TERM

echo "run,lane,node,interface,status,t0_epoch,active_iso,detect_s,for_s,fire_s,wf_started_iso,wf_finished_iso,notify_s,enrich_s,wf_dur_s" > "$OUT"

for ((run=1; run<=N; run++)); do
  token=${IFACE_LIST[$(( (run-1) % ${#IFACE_LIST[@]} ))]}
  NODE=${token%%:*}; INTERFACE=${token#*:}
  echo "--- run $run/$N  ($NODE/$INTERFACE) ---"
  PRE_WFS=$(kubectl -n argo-events get wf -o name 2>/dev/null | sort)

  T0=$(date +%s)
  echo "    [$(date +%H:%M:%S)] cut $NODE/$INTERFACE"
  CUT

  # Hop 1: wait for the matching alert to FIRE; record its (exact) activeAt.
  ACTIVE_ISO=""; STATE=""
  while (( $(date +%s) - T0 < TIMEOUT )); do
    line=$(prom_alerts | match_alert)
    if [[ -n "$line" ]]; then
      ACTIVE_ISO=${line%%$'\t'*}; STATE=${line##*$'\t'}
      [[ "$STATE" == "firing" ]] && break
    fi
    sleep 1
  done
  if [[ "$STATE" != "firing" || -z "$ACTIVE_ISO" ]]; then
    echo "    TIMEOUT waiting for $ALERTNAME firing" >&2
    echo "$run,$LANE,$NODE,$INTERFACE,timeout-alert,$T0,,,,,,,,," >> "$OUT"
    RESTORE; sleep "$SETTLE_BUFFER"; continue
  fi
  active_epoch=$(iso2epoch "$ACTIVE_ISO")
  detect_s=$(python3 -c "print(round($active_epoch - $T0, 1))")
  fire_s=$(python3 -c "print(round($active_epoch - $T0 + $FOR_S, 1))")
  echo "    detect_s=$detect_s  fire_s=$fire_s (activeAt=$ACTIVE_ISO)"

  # Hop 2: new enrich-notify-* workflow reaches Succeeded; read exact timestamps.
  WF=""; WF_START_ISO=""; WF_FIN_ISO=""
  while (( $(date +%s) - T0 < TIMEOUT )); do
    NEW=$(comm -13 <(echo "$PRE_WFS") <(kubectl -n argo-events get wf -o name 2>/dev/null | sort) \
          | grep "^workflow.argoproj.io/enrich-notify-" || true)
    while read -r w; do
      [[ -z "$w" ]] && continue
      ph=$(kubectl -n argo-events get "$w" -o jsonpath='{.status.phase}' 2>/dev/null || true)
      if [[ "$ph" == "Succeeded" ]]; then
        WF=$w
        WF_START_ISO=$(kubectl -n argo-events get "$w" -o jsonpath='{.status.startedAt}' 2>/dev/null)
        WF_FIN_ISO=$(kubectl   -n argo-events get "$w" -o jsonpath='{.status.finishedAt}' 2>/dev/null)
        break 2
      fi
    done <<<"$NEW"
    sleep 1
  done
  if [[ -z "$WF_FIN_ISO" ]]; then
    echo "    TIMEOUT waiting for enrich-notify Succeeded" >&2
    echo "$run,$LANE,$NODE,$INTERFACE,timeout-wf,$T0,$ACTIVE_ISO,$detect_s,$FOR_S,$fire_s,,,,," >> "$OUT"
    RESTORE; sleep "$SETTLE_BUFFER"; continue
  fi
  fin_epoch=$(iso2epoch "$WF_FIN_ISO")
  start_epoch=$(iso2epoch "$WF_START_ISO")
  notify_s=$(python3 -c "print(round($fin_epoch - $T0, 1))")
  enrich_s=$(python3 -c "print(round($fin_epoch - $T0 - ($active_epoch - $T0 + $FOR_S), 1))")
  wf_dur_s=$(python3 -c "print(round($fin_epoch - $start_epoch, 1))")
  echo "    notify_s=$notify_s  enrich_s=$enrich_s  wf_dur_s=$wf_dur_s ($WF)"

  echo "$run,$LANE,$NODE,$INTERFACE,ok,$T0,$ACTIVE_ISO,$detect_s,$FOR_S,$fire_s,$WF_START_ISO,$WF_FIN_ISO,$notify_s,$enrich_s,$wf_dur_s" >> "$OUT"

  # Restore + settle so the next run starts from a clean, resolved state.
  RESTORE
  echo "    restoring + settling"
  while (( $(date +%s) - T0 < TIMEOUT )); do
    [[ -z "$(prom_alerts | match_alert)" ]] && break
    sleep 2
  done
  sleep "$SETTLE_BUFFER"
done

trap - EXIT INT TERM
echo
echo "==> wrote $OUT"
echo "==> summary (successful runs only)"
OUT="$OUT" LANE="$LANE" python3 -c '
import csv, os, statistics as st
rows = [r for r in csv.DictReader(open(os.environ["OUT"])) if r["status"] == "ok"]
cols = ["detect_s","fire_s","notify_s","enrich_s","wf_dur_s"]
n = len(rows)
print("    lane=" + os.environ["LANE"] + "  n_ok=" + str(n))
if not n:
    print("    (no successful runs)"); raise SystemExit
def pct(xs, p):
    xs = sorted(xs); k = max(0, min(len(xs)-1, round((p/100)*(len(xs)-1))))
    return xs[k]
hdr = ["metric","mean","median","p95","min","max"]
print("    " + "{:<10} {:>7} {:>7} {:>7} {:>7} {:>7}".format(*hdr))
for c in cols:
    xs = [float(r[c]) for r in rows if r[c] not in ("", None)]
    if not xs: continue
    print("    " + "{:<10} {:>7.1f} {:>7.1f} {:>7.1f} {:>7.1f} {:>7.1f}".format(
        c, st.mean(xs), st.median(xs), pct(xs,95), min(xs), max(xs)))
'
