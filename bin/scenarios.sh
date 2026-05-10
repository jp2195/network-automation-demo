#!/usr/bin/env bash
# bin/scenarios.sh — pre-canned demo outage scripts.
#
# Each scenario logs what it's doing in real time, sleeps between
# steps so the dashboards have time to react, and auto-restores on
# completion / interrupt.
#
# Usage:
#   bin/scenarios.sh list
#   bin/scenarios.sh hurricane
#   bin/scenarios.sh backhoe
#   bin/scenarios.sh cabinet-loss
#   bin/scenarios.sh flapping
#
# Wrap with `make scenario-<name>` for ergonomics.

set -uo pipefail

TOPO_NS="${TOPO_NS:-clabernetes}"
COLOR="${COLOR:-1}"

if [[ "$COLOR" == 1 && -t 1 ]]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; BOLD=$'\033[1m'; CLR=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; CLR=""
fi

log()    { printf "%s %s\n" "$(date +%H:%M:%S)" "$*"; }
banner() { printf "\n${BOLD}${BLUE}== %s ==${CLR}\n" "$*"; }
warn()   { printf "${YELLOW}!! %s${CLR}\n" "$*"; }
ok()     { printf "${GREEN}✓  %s${CLR}\n" "$*"; }
hot()    { printf "${RED}🔥 %s${CLR}\n" "$*"; }

# Pairs of (node, interface) to act on. Filled by each scenario; the
# trap restores everything in reverse order on exit.
RESTORE_QUEUE=()

push_restore() { RESTORE_QUEUE+=("$1:$2"); }

cut() {
  local node=$1 intf=$2
  hot "cutting ${BOLD}${node}/${intf}${CLR}"
  make -s demo-cut NODE="$node" INTERFACE="$intf" >/dev/null
  push_restore "$node" "$intf"
}

restore() {
  local node=$1 intf=$2
  ok "restoring ${BOLD}${node}/${intf}${CLR}"
  make -s demo-restore NODE="$node" INTERFACE="$intf" >/dev/null || true
}

cleanup() {
  if (( ${#RESTORE_QUEUE[@]} > 0 )); then
    banner "scenario cleanup — restoring ${#RESTORE_QUEUE[@]} cuts"
    # restore in reverse so a partially-applied scenario un-winds in the
    # opposite order it was applied
    for (( i=${#RESTORE_QUEUE[@]}-1; i>=0; i-- )); do
      IFS=: read -r n iface <<<"${RESTORE_QUEUE[$i]}"
      restore "$n" "$iface"
    done
  fi
}
trap cleanup EXIT INT TERM

# ────────────────────────────────────────────────────────────────────────
# scenario: hurricane
# Atlanta gets hit. Two ring segments fail in series, with a window
# where the network is actively healing. Auto-restores in reverse.
# ────────────────────────────────────────────────────────────────────────
scenario_hurricane() {
  banner "scenario: hurricane"
  log "1/4  ring-e-i20e drops (storm surge)"
  cut hub-i20e ethernet-1/2
  log "     dashboards should show oper_state=2 within 5s, alert pending in 30s"
  sleep 30

  log "2/4  ring-i20e-sw drops too — corridor isolated, fc-i20e is now stranded"
  cut hub-i20e ethernet-1/1
  log "     watch the alert console fill in — analyze step should flag fc-i20e in downstream_devices"
  sleep 60

  log "3/4  recovery — ring-i20e-sw repaired first"
  restore hub-i20e ethernet-1/1
  RESTORE_QUEUE=("${RESTORE_QUEUE[@]/hub-i20e:ethernet-1\/1/}")
  RESTORE_QUEUE=(${RESTORE_QUEUE[@]:-})
  sleep 30

  log "4/4  ring-e-i20e back up — full restoration"
  restore hub-i20e ethernet-1/2
  RESTORE_QUEUE=()  # nothing left to clean up

  ok "hurricane scenario complete"
}

# ────────────────────────────────────────────────────────────────────────
# scenario: backhoe
# Single random backbone link goes down for ~2 minutes. The simplest
# narrative — one fiber strand cut by construction.
# ────────────────────────────────────────────────────────────────────────
scenario_backhoe() {
  banner "scenario: backhoe"
  # Pick a random backbone endpoint.
  local pairs=(
    "hub-n:ethernet-1/2"      # ring-nw-n
    "hub-e:ethernet-1/2"      # ring-n-e
    "hub-i20e:ethernet-1/2"   # ring-e-i20e
    "hub-sw:ethernet-1/2"     # ring-i20e-sw
    "hub-i20w:ethernet-1/2"   # ring-sw-i20w
    "hub-nw:ethernet-1/2"     # ring-i20w-nw
  )
  local pick=${pairs[$RANDOM % ${#pairs[@]}]}
  IFS=: read -r node intf <<<"$pick"

  log "construction equipment severed a backbone strand"
  cut "$node" "$intf"
  log "     waiting 120s — alert console + geomap should show one red link"
  sleep 120

  log "ATSP dispatched, repair complete"
  restore "$node" "$intf"
  RESTORE_QUEUE=()
  ok "backhoe scenario complete"
}

# ────────────────────────────────────────────────────────────────────────
# scenario: cabinet-loss
# A field cabinet's hub-facing interface goes down. SNMP probe still
# works (the cabinet itself is fine), but the SR Linux side flags the
# link, and the workflow correctly identifies the cabinet as the
# downstream impact.
# ────────────────────────────────────────────────────────────────────────
scenario_cabinet_loss() {
  banner "scenario: cabinet-loss"
  log "fc-n cabinet uplink failure (hub-n side oper-down)"
  cut hub-n ethernet-1/4
  log "     wait 90s — analyze step should set affected_device=hub-n,"
  log "     downstream_devices includes fc-n"
  sleep 90

  log "uplink restored"
  restore hub-n ethernet-1/4
  RESTORE_QUEUE=()
  ok "cabinet-loss scenario complete"
}

# ────────────────────────────────────────────────────────────────────────
# scenario: flapping
# Rapid up/down on a single interface to trip SRLInterfaceFlapping.
# Tests the >4 changes / 5 minutes alert path.
# ────────────────────────────────────────────────────────────────────────
scenario_flapping() {
  banner "scenario: flapping"
  local node=hub-e intf=ethernet-1/1
  log "flapping ${node}/${intf} 6x to trip the SRLInterfaceFlapping alert"
  for i in 1 2 3 4 5 6; do
    log "  flap $i/6 — down"
    make -s demo-cut NODE="$node" INTERFACE="$intf" >/dev/null
    sleep 10
    log "  flap $i/6 — up"
    make -s demo-restore NODE="$node" INTERFACE="$intf" >/dev/null
    sleep 10
  done
  log "settled — wait 60s for SRLInterfaceFlapping to enter firing"
  sleep 60
  ok "flapping scenario complete"
}

# ────────────────────────────────────────────────────────────────────────
list_scenarios() {
  cat <<EOF
Available scenarios:

  hurricane      Two ring segments fail in series with a 60s window
                 where the network is actively healing.
                 Total: ~2.5 minutes.

  backhoe        Single random backbone strand cut for ~2 minutes.
                 Total: ~2 minutes.

  cabinet-loss   Field cabinet uplink failure (hub side).
                 Total: ~1.5 minutes.

  flapping       Rapid up/down to trip SRLInterfaceFlapping (>4 changes/5min).
                 Total: ~3 minutes.

All scenarios auto-restore on completion or interrupt (Ctrl-C).
EOF
}

main() {
  local cmd=${1:-list}
  shift || true
  case "$cmd" in
    list)            list_scenarios ;;
    hurricane)       scenario_hurricane ;;
    backhoe)         scenario_backhoe ;;
    cabinet-loss)    scenario_cabinet_loss ;;
    flapping)        scenario_flapping ;;
    *)               warn "unknown scenario: $cmd"; list_scenarios; exit 1 ;;
  esac
}

main "$@"
