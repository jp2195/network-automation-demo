#!/usr/bin/env bash
# bin/ready.sh — functional readiness gate for the Atlas demo.
#
# ArgoCD "Healthy" means the manifests reconciled; it does NOT mean the lab is
# functionally ready (telemetry flowing, eventing wired, cabinets polling). This
# gate checks the things that actually have to work before a demo or a
# measurement run, prints a green/red checklist, and exits non-zero if any fail.
#
# Usage:  bin/ready.sh   (or: make ready)
# Exit:   0 = all checks pass; 1 = at least one functional check failed.
#
# Designed to be safe to run repeatedly and to work headless (no Grafana).

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

PASS=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

PP=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus \
       -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
# promcount <urlencoded-query> -> number of series (0 on any failure)
promcount() {
  [ -n "$PP" ] || { echo 0; return; }
  kubectl -n monitoring exec "$PP" -c prometheus -- \
    wget -qO- "http://localhost:9090/api/v1/query?query=$1" 2>/dev/null \
    | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)["data"]["result"]))
except Exception: print(0)' 2>/dev/null || echo 0
}
promsum() {  # sum of values (e.g. count of up==1 targets)
  [ -n "$PP" ] || { echo 0; return; }
  kubectl -n monitoring exec "$PP" -c prometheus -- \
    wget -qO- "http://localhost:9090/api/v1/query?query=$1" 2>/dev/null \
    | python3 -c 'import json,sys
try: print(int(sum(float(r["value"][1]) for r in json.load(sys.stdin)["data"]["result"])))
except Exception: print(0)' 2>/dev/null || echo 0
}

echo "==> Atlas demo readiness"

# 1. ArgoCD — every Application Synced + Healthy
appline=$(kubectl -n argocd get applications \
  -o custom-columns='S:.status.sync.status,H:.status.health.status' --no-headers 2>/dev/null)
if [ -z "$appline" ]; then
  bad "argocd: no Applications found (is the cluster up / root app applied?)"
else
  total=$(printf '%s\n' "$appline" | grep -c .)
  ready=$(printf '%s\n' "$appline" | grep -c 'Synced  *Healthy')
  if [ "$ready" -eq "$total" ]; then ok "argocd: $ready/$total apps Synced+Healthy"
  else bad "argocd: $ready/$total apps Synced+Healthy ($((total-ready)) still converging)"; fi
fi

# 2. Eventing data plane (the inotify-sensitive path). Cross-platform: instead of
#    reading a host sysctl (meaningless on macOS), check the SYMPTOM — the NATS
#    eventbus is Ready and no sensor/eventsource pod is crashlooping right now.
#    Scope to INFRA pods only (!workflows.argoproj.io/workflow): a failed or old
#    Argo *workflow* pod sits in Error/Completed and must not be miscounted as a
#    crashlooping sensor.
ev=$(kubectl -n argo-events get pods -l '!workflows.argoproj.io/workflow' --no-headers 2>/dev/null)
if [ -z "$ev" ]; then
  bad "eventing: no pods in argo-events (cut→notify automation will never fire)"
else
  crash=$(printf '%s\n' "$ev" | grep -icE 'CrashLoopBackOff|Error|ImagePullBackOff')
  ebtotal=$(printf '%s\n' "$ev" | grep -c 'eventbus-default-js')
  ebready=$(printf '%s\n' "$ev" | grep 'eventbus-default-js' | awk '{split($2,a,"/"); if(a[1]==a[2]) c++} END{print c+0}')
  if [ "$crash" -gt 0 ]; then
    bad "eventing: $crash pod(s) crashlooping — likely fs.inotify.max_user_instances too low (raise to ≥1024)"
  elif [ "$ebready" -lt "$ebtotal" ] || [ "$ebtotal" -eq 0 ]; then
    bad "eventing: NATS eventbus not Ready ($ebready/$ebtotal) — sensors can't receive events yet"
  else
    ok "eventing: eventbus Ready ($ebready/$ebtotal), no crashlooping sensors"
  fi
fi

# 3. gNMI lane — streaming telemetry flowing + link-membership join populated
srl=$(promcount 'srl_nokia_interfaces_interface_oper_state')
lm=$(promcount 'link_membership_info')
if [ "$srl" -gt 0 ] && [ "$lm" -gt 0 ]; then
  ok "gnmi: $srl oper-state series, $lm link_membership_info"
else
  bad "gnmi: oper-state=$srl link_membership=$lm (gNMIc not scraped / SRL nodes not booted)"
fi

# 4. SNMP lane — all cabinet targets up AND ifOperStatus actually populated.
#    This is the check that catches a cabinet whose snmpd never started.
snmpseries=$(promcount 'ifOperStatus%7Btelemetry_source%3D%22snmp%22%7D')
snmpup=$(promsum 'up%7Bjob%3D%22snmp-frr-cabinets%22%7D')
snmptot=$(promcount 'up%7Bjob%3D%22snmp-frr-cabinets%22%7D')
if [ "$snmpup" -gt 0 ] && [ "$snmpup" -eq "$snmptot" ] && [ "$snmpseries" -gt 0 ]; then
  ok "snmp: $snmpup/$snmptot cabinets up, $snmpseries ifOperStatus series"
else
  bad "snmp: $snmpup/$snmptot cabinets up, $snmpseries ifOperStatus series (snmpd down on cabinet(s)?)"
fi

# 5. NetBox — seed Job complete (enrichment lookups depend on it)
seed=$(kubectl -n netbox get job netbox-seed -o jsonpath='{.status.succeeded}' 2>/dev/null)
if [ "${seed:-0}" -ge 1 ]; then ok "netbox: seed Complete"
else bad "netbox: seed not Complete (enrichment will degrade to name-only)"; fi

# 6. Topology — all emulated nodes' pods Running
topo=$(kubectl -n clabernetes get pods -l clabernetes/topologyOwner=atlanta --no-headers 2>/dev/null)
if [ -z "$topo" ]; then
  bad "topology: no atlanta nodes found in clabernetes namespace"
else
  ttot=$(printf '%s\n' "$topo" | grep -c .)
  trun=$(printf '%s\n' "$topo" | awk '{split($2,a,"/"); if(a[1]==a[2] && $3=="Running") c++} END{print c+0}')
  if [ "$trun" -eq "$ttot" ]; then ok "topology: $trun/$ttot nodes Running"
  else bad "topology: $trun/$ttot nodes Running"; fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mREADY\033[0m — %d checks passed\n' "$PASS"
  exit 0
else
  printf '\033[31mNOT READY\033[0m — %d failed, %d passed\n' "$FAIL" "$PASS"
  exit 1
fi
