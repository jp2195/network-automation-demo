# Shared helpers for bin/*.sh — source with:
#   . "$(dirname "$0")/lib.sh"
# shellcheck shell=bash

# podget NAMESPACE POD PORT PATH — HTTP GET against a pod through the API
# server's pod proxy. Prometheus and Alertmanager ship distroless images (no
# wget/curl/sleep to `kubectl exec`), and this needs no port-forward either.
podget() { kubectl get --raw "/api/v1/namespaces/$1/pods/$2:$3/proxy$4"; }

# Repository root (bin/..), independent of the caller's working directory.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
