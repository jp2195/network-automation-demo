#!/usr/bin/env bash
# bin/maintenance.sh — open / close Atlas-DOT maintenance windows.
#
# Submits a Workflow against the maintenance-on / maintenance-off
# WorkflowTemplate. The Workflow posts an Alertmanager silence keyed
# on `node=<NODE>` and writes a NetBox journal entry on the device.
# Alertmanager auto-expires the silence at endsAt — no cron required.
#
# Usage:
#   bin/maintenance.sh start <node> [hours] [comment]
#   bin/maintenance.sh end   <node>
#   bin/maintenance.sh list

set -uo pipefail

usage() {
  cat <<EOF
Usage:
  $0 start <node> [hours] [comment]
  $0 end   <node>
  $0 list

Examples:
  $0 start hub-e 2 "fiber splice tomorrow"
  $0 end   hub-e
EOF
}

start() {
  local node=${1:-}
  local hours=${2:-2}
  local comment=${3:-scheduled maintenance}
  if [[ -z "$node" ]]; then usage; exit 1; fi
  cat <<YAML | kubectl -n argo-events create -f -
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: maintenance-on-
spec:
  workflowTemplateRef:
    name: maintenance-on
  arguments:
    parameters:
      - name: node
        value: "${node}"
      - name: duration_hours
        value: "${hours}"
      - name: comment
        value: "${comment}"
YAML
}

end() {
  local node=${1:-}
  if [[ -z "$node" ]]; then usage; exit 1; fi
  cat <<YAML | kubectl -n argo-events create -f -
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: maintenance-off-
spec:
  workflowTemplateRef:
    name: maintenance-off
  arguments:
    parameters:
      - name: node
        value: "${node}"
YAML
}

list() {
  local am
  am=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
  if [[ -z "$am" ]]; then echo "alertmanager pod not found" >&2; exit 1; fi
  printf "%-10s  %-14s  %-26s  %s\n" "id" "node" "endsAt" "comment"
  kubectl -n monitoring exec "$am" -c alertmanager -- wget -qO- 'http://localhost:9093/api/v2/silences' \
    | jq -r '
        .[]
        | select(.createdBy == "atlas-maintenance")
        | select(.status.state == "active")
        | [
            (.id[0:10]),
            ((.matchers[] | select(.name == "node") | .value) // "-"),
            .endsAt,
            (.comment // "")
          ]
        | @tsv
      ' \
    | awk -F'\t' '{ printf "%-10s  %-14s  %-26s  %s\n", $1, $2, $3, $4 }'
}

cmd=${1:-}
shift || true
case "$cmd" in
  start) start "$@" ;;
  end)   end   "$@" ;;
  list)  list ;;
  *)     usage; exit 1 ;;
esac
