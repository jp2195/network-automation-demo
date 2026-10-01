#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-argocd}"
RELEASE="${RELEASE:-argocd}"
CHART_VERSION="${ARGOCD_CHART_VERSION:-10.9.6}"
HOSTNAME="${ARGOCD_HOSTNAME:-argocd.127-0-0-1.nip.io}"

echo "==> Ensuring argo helm repo"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

echo "==> Installing argo-cd chart ${CHART_VERSION} into ns ${NAMESPACE}"
# *.metrics expose the argocd_* metrics Services. No chart ServiceMonitor:
# the chart only renders one when the monitoring.coreos.com CRDs exist, and
# they don't at bootstrap (kube-prometheus-stack is synced BY ArgoCD later).
# workloads/observability/argocd-servicemonitor.yaml scrapes them instead.
helm upgrade --install "${RELEASE}" argo/argo-cd \
  --version "${CHART_VERSION}" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --set 'configs.params.server\.insecure=true' \
  --set server.ingress.enabled=true \
  --set server.ingress.ingressClassName=traefik \
  --set server.ingress.hostname="${HOSTNAME}" \
  --set controller.metrics.enabled=true \
  --set server.metrics.enabled=true \
  --set repoServer.metrics.enabled=true \
  --set applicationSet.metrics.enabled=true \
  --wait --timeout 10m

# Tolerate a deleted initial secret so re-running `make up` stays idempotent.
PASSWORD=$(kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null) \
  || PASSWORD="(initial secret deleted — use the password you set)"

cat <<EOF

=================================================================
  ArgoCD URL:      http://${HOSTNAME}:8080
  ArgoCD username: admin
  ArgoCD password: ${PASSWORD}
=================================================================
EOF
