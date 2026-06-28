#!/usr/bin/env bash
# bin/measure-gray.sh — gray-failure detectability: streaming vs 5-min polling vs traps.
#
# A gray failure (optical Rx-power degradation here) is a RAMP, not a step, so
# "fault->alert latency" isn't the right metric. Instead we inject a controlled
# degradation of known duration, measure when the STREAMING alert really fires,
# capture the high-resolution signal Prometheus actually saw, and DERIVE the
# polling/trap counterfactual from that same signal:
#
#   streaming : measured — SRLOpticalDegrading (dom_rx_power_dbm < -12, for:2m),
#               scraped every 15s.
#   polling   : decimate the captured signal to a 300s sampler and sweep the
#               sample phase across [0,300): the fraction of phases that land a
#               sample inside the below-threshold window is P(detect). With a
#               5-min poll and a for:2m debounce, short degradations are usually
#               stepped over entirely.
#   traps     : N/A — there is no SNMP trap for a gauge crossing (only RMON,
#               effectively never deployed), so gray failures are invisible: 0%.
#
# Sweeping the failure duration yields a detection-probability-vs-duration curve.
#
# Usage:
#   bin/measure-gray.sh [-D "180 360 600"] [-l "ring-n-e ring-e-i20e ..."]
#                       [-p 300] [-r 15] [-o out.csv]
#   -D  durations (s) to sweep (default "180 360 600")
#   -l  ring link_ids to use, one per duration, cycled (default: discovered)
#   -p  polling interval to model, seconds (default 300 = 5 min)
#   -r  Rx offset dBm at peak (default 15 → -3 dBm baseline crosses -12)
#   -o  output CSV (default results/measure-gray-<p>poll.csv)
#
# Requires an up cluster with monitoring + dom-synth + valkey deployed.

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

DURATIONS="180 360 600"
LINKS=""
POLL=300
RXOFF=15
OUT=""
THRESH=-12       # SRLOpticalDegrading: dom_rx_power_dbm < -12
FOR_S=120        # SRLOpticalDegrading: for: 2m
SCRAPE=15        # dom-synth ServiceMonitor interval

while getopts ":D:l:p:r:o:h" opt; do
  case "$opt" in
    D) DURATIONS=$OPTARG ;;
    l) LINKS=$OPTARG ;;
    p) POLL=$OPTARG ;;
    r) RXOFF=$OPTARG ;;
    o) OUT=$OPTARG ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag -$OPTARG (try -h)" >&2; exit 64 ;;
  esac
done
: "${OUT:=results/measure-gray-${POLL}poll.csv}"
mkdir -p "$(dirname "$OUT")"

PP=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus \
       -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
VPOD=$(kubectl -n valkey get pod -l app.kubernetes.io/name=valkey -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -z "$VPOD" ] && VPOD=$(kubectl -n valkey get pod -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$PP" ] || [ -z "$VPOD" ]; then
  echo "need prometheus (monitoring) + valkey pods; is the cluster up?" >&2; exit 1
fi

promget() { kubectl -n monitoring exec "$PP" -c prometheus -- wget -qO- "http://localhost:9090$1" 2>/dev/null; }
vcli()    { kubectl -n valkey exec "$VPOD" -c valkey -- valkey-cli "$@" 2>/dev/null; }

# Discover ring (backbone) link_ids if none supplied.
if [ -z "$LINKS" ]; then
  LINKS=$(promget "/api/v1/query?query=link_membership_info" | python3 -c '
import json,sys
r=json.load(sys.stdin)["data"]["result"]
ids=sorted({m["metric"].get("link_id") for m in r if m["metric"].get("link_kind")=="backbone"})
print(" ".join(i for i in ids if i))')
fi
read -r -a LINK_ARR <<<"$LINKS"
[ "${#LINK_ARR[@]}" -eq 0 ] && { echo "no ring link_ids found" >&2; exit 1; }

echo "==> gray-failure sweep: durations=[$DURATIONS] poll=${POLL}s rxoff=${RXOFF}dBm thresh=${THRESH} for=${FOR_S}s -> $OUT"
echo "    links: ${LINK_ARR[*]}"

cleanup() { for l in "${LINK_ARR[@]}"; do vcli -n 3 DEL "gray:$l" >/dev/null; done; }
trap cleanup EXIT INT TERM

echo "run,link,duration_s,dwell_below_s,stream_fires,stream_signal_s,stream_fire_s,poll_p_detect,poll_mean_detect_s,trap_detect" > "$OUT"

run=0
for D in $DURATIONS; do
  run=$((run+1))
  link=${LINK_ARR[$(( (run-1) % ${#LINK_ARR[@]} ))]}
  echo "--- run $run: link=$link duration=${D}s ---"

  T0=$(date +%s)
  vcli -n 3 SET "gray:$link" \
    "{\"start_ts\":$T0,\"duration_s\":$D,\"peak_rx_offset_dbm\":$RXOFF,\"peak_errors_per_sec\":120}" \
    EX $((D + FOR_S + 60)) >/dev/null
  echo "    [$(date +%H:%M:%S)] injected gray:$link"

  # Measure the real streaming alert: poll until SRLOpticalDegrading fires for this link.
  STREAM_FIRE_S=""; ACTIVE_ISO=""
  deadline=$((T0 + D + FOR_S + 60))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    line=$(promget "/api/v1/alerts" | LINK="$link" python3 -c '
import json,os,sys
link=os.environ["LINK"]
try: alerts=json.load(sys.stdin)["data"]["alerts"]
except Exception: sys.exit(0)
for a in alerts:
    l=a.get("labels",{})
    if l.get("alertname")=="SRLOpticalDegrading" and l.get("link_id")==link:
        print(a.get("activeAt","")+"\t"+a.get("state",""))
        break')
    if [ -n "$line" ]; then
      ACTIVE_ISO=${line%%$'\t'*}; st=${line##*$'\t'}
      [ "$st" = "firing" ] && break
    fi
    sleep 5
  done

  # Let the ramp finish, then clear and capture the full high-res signal.
  now=$(date +%s); end_inject=$((T0 + D + 15))
  [ "$now" -lt "$end_inject" ] && kubectl -n monitoring exec "$PP" -c prometheus -- sleep $((end_inject - now)) 2>/dev/null
  vcli -n 3 DEL "gray:$link" >/dev/null

  q=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "min(dom_rx_power_dbm{link_id=\"$link\"})")
  series=$(promget "/api/v1/query_range?query=${q}&start=$((T0-30))&end=$((T0+D+60))&step=${SCRAPE}")

  row=$(echo "$series" | T0="$T0" D="$D" THRESH="$THRESH" FOR_S="$FOR_S" POLL="$POLL" SCRAPE="$SCRAPE" \
        ACTIVE_ISO="$ACTIVE_ISO" LINK="$link" RUN="$run" python3 -c '
import json,os,sys,datetime
T0=float(os.environ["T0"]); D=float(os.environ["D"]); TH=float(os.environ["THRESH"])
FOR=float(os.environ["FOR_S"]); POLL=float(os.environ["POLL"]); STEP=float(os.environ["SCRAPE"])
link=os.environ["LINK"]; run=os.environ["RUN"]; active=os.environ["ACTIVE_ISO"].strip()

def iso2epoch(s):
    s=s.replace("Z","+00:00")
    if "." in s:
        head,rest=s.split(".",1); frac="".join(c for c in rest if c.isdigit())[:6]
        tz=""
        for i,c in enumerate(rest):
            if c in "+-" and i>0: tz=rest[i:]; break
        s=head+"."+frac+tz
    return datetime.datetime.fromisoformat(s).timestamp()

try:
    res=json.load(sys.stdin)["data"]["result"]
    vals=res[0]["values"] if res else []
except Exception:
    vals=[]
# time series relative to T0: list of (t_rel, value)
ts=[(float(t)-T0, float(v)) for t,v in vals]

# below-threshold dwell (contiguous span where v < TH)
below=[t for t,v in ts if v < TH]
dwell = (max(below)-min(below)) if below else 0.0

# streaming: measured activeAt + for (only if it actually fired)
if active:
    sig = iso2epoch(active)-T0
    stream_fires=1; stream_sig=round(sig,1); stream_fire=round(sig+FOR,1)
else:
    stream_fires=0; stream_sig=""; stream_fire=""

# polling counterfactual: sweep sample phase across [0,POLL); a phase "detects"
# if any of its samples (phase, phase+POLL, ...) lands on a below-threshold value.
# Value at a sample time = nearest captured sample (staleness carries last value).
def val_at(t):
    best=None
    for tt,v in ts:
        if tt<=t: best=v
        else: break
    return best
phases=int(POLL/STEP) if STEP>0 else 1
detect=0; delays=[]
span_end=D+60
for k in range(max(1,phases)):
    ph=k*STEP
    hit=None; t=ph
    while t<=span_end:
        v=val_at(t)
        if v is not None and v<TH: hit=t; break
        t+=POLL
    if hit is not None:
        detect+=1
        # once a sub-threshold sample is caught, staleness holds it through the
        # for: window, so the alert fires ~FOR later.
        delays.append(hit+FOR)
p_detect=round(detect/max(1,phases),2)
mean_delay=round(sum(delays)/len(delays),1) if delays else ""

print(",".join(str(x) for x in
   [run,link,int(D),round(dwell,1),stream_fires,stream_sig,stream_fire,p_detect,mean_delay,0]))
')
  echo "$row" >> "$OUT"
  echo "    $row"

  # settle: let alert resolve before next run
  kubectl -n monitoring exec "$PP" -c prometheus -- sleep 20 2>/dev/null
done

trap - EXIT INT TERM
cleanup
echo
echo "==> wrote $OUT"
echo "==> gray-failure detectability (streaming measured; polling/trap derived)"
column -t -s, "$OUT" 2>/dev/null || cat "$OUT"
