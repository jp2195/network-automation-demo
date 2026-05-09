#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-argocd}"
RELEASE="${RELEASE:-argocd}"
CHART_VERSION="${ARGOCD_CHART_VERSION:-9.5.13}"
HOSTNAME="${ARGOCD_HOSTNAME:-argocd.127-0-0-1.nip.io}"

echo "==> Ensuring argo helm repo"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

echo "==> Installing argo-cd chart ${CHART_VERSION} into ns ${NAMESPACE}"
helm upgrade --install "${RELEASE}" argo/argo-cd \
  --version "${CHART_VERSION}" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --set 'configs.params.server\.insecure=true' \
  --set server.ingress.enabled=true \
  --set server.ingress.ingressClassName=traefik \
  --set server.ingress.hostname="${HOSTNAME}" \
  --wait --timeout 10m

echo "==> Waiting for argocd-server rollout"
kubectl -n "${NAMESPACE}" rollout status deploy/"${RELEASE}"-server --timeout=5m

PASSWORD=$(kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d)

cat <<EOF

=================================================================
  ArgoCD URL:      http://${HOSTNAME}:8080
  ArgoCD username: admin
  ArgoCD password: ${PASSWORD}
=================================================================
EOF
