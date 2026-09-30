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

# Node names are plain hostnames; reject anything else up front.
check_node() {
  if [[ ! "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    echo "invalid node name: $1" >&2
    exit 1
  fi
}

# Build the Workflow as JSON with jq --arg so node/comment values are
# always properly escaped (JSON is valid YAML for `kubectl create -f -`),
# instead of interpolating raw strings into a YAML heredoc.
submit() {
  local template=$1; shift
  jq -n --arg tmpl "$template" "$@" '{
    apiVersion: "argoproj.io/v1alpha1",
    kind: "Workflow",
    metadata: {generateName: ($tmpl + "-")},
    spec: {
      workflowTemplateRef: {name: $tmpl},
      arguments: {parameters: [$ARGS.named | to_entries[]
        | select(.key != "tmpl") | {name: .key, value: .value}]}
    }
  }' | kubectl -n argo-events create -f -
}

start() {
  local node=${1:-}
  local hours=${2:-2}
  local comment=${3:-scheduled maintenance}
  if [[ -z "$node" ]]; then usage; exit 1; fi
  check_node "$node"
  if [[ ! "$hours" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "invalid hours: $hours" >&2
    exit 1
  fi
  submit maintenance-on --arg node "$node" --arg duration_hours "$hours" --arg comment "$comment"
}

end() {
  local node=${1:-}
  if [[ -z "$node" ]]; then usage; exit 1; fi
  check_node "$node"
  submit maintenance-off --arg node "$node"
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
