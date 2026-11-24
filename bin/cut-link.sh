#!/usr/bin/env bash
# bin/cut-link.sh — simulate a fiber cut (carrier loss) on one lab interface.
#
# Usage:
#   bin/cut-link.sh NODE INTERFACE down|up
#   e.g. bin/cut-link.sh hub-e ethernet-1/2 down
#        bin/cut-link.sh fc-n eth1 down
#
# Clabernetes (>= 0.9) carries each link as a "wire" between the two device
# pods, and the wire behaves like a cable: when one device takes its end of
# the link down, the peer sees loss of carrier while its own interface stays
# administratively up. So to cut NODE's INTERFACE we take the FAR end's
# kernel interface down. NODE then shows exactly what a fiber cut looks like
# — oper-down with admin-state still enabled (SR Linux: no config change,
# nothing in the AAA log; FRR cabinet: ifOperStatus down with ifAdminStatus
# up, the condition CabinetInterfaceOperDown requires). The far end goes
# oper-down too, as both ends of a real cut fiber do.
#
# The peer is looked up from the topology's Link resources (links.c9s.run),
# so this works for any modeled link. `up` restores it.
set -euo pipefail

NODE="${1:-}"; INTERFACE="${2:-}"; ACTION="${3:-}"
NS="${TOPO_NS:-clabernetes}"
case "$ACTION" in down|up) ;; *)
  echo "usage: $0 NODE INTERFACE down|up" >&2; exit 2 ;;
esac
[ -n "$NODE" ] && [ -n "$INTERFACE" ] || { echo "usage: $0 NODE INTERFACE down|up" >&2; exit 2; }

# SR Linux ethernet-1/4 is kernel interface e1-4; FRR's eth1 is already a
# kernel name.
IF=$(printf '%s' "$INTERFACE" | sed -E 's#^ethernet-([0-9]+)/([0-9]+)$#e\1-\2#')

peer=$(kubectl -n "$NS" get links.c9s.run -o jsonpath='{range .items[*]}{.spec.endpointA.nodeName} {.spec.endpointA.interfaceName} {.spec.endpointB.nodeName} {.spec.endpointB.interfaceName}{"\n"}{end}' \
  | awk -v n="$NODE" -v i="$IF" '$1==n && $2==i {print $3, $4} $3==n && $4==i {print $1, $2}')
if [ -z "$peer" ]; then
  echo "no modeled link on $NODE $INTERFACE (kernel name $IF) in namespace $NS — is the topology deployed?" >&2
  exit 1
fi
read -r PEER_NODE PEER_IF <<<"$peer"

if [ "$ACTION" = down ]; then
  echo "==> Fiber cut on $NODE $INTERFACE: taking far end $PEER_NODE $PEER_IF down (carrier loss; admin-state stays up)"
else
  echo "==> Restoring $NODE $INTERFACE: bringing far end $PEER_NODE $PEER_IF back up"
fi
kubectl -n "$NS" exec "deploy/$PEER_NODE" -- ip link set "$PEER_IF" "$ACTION"
