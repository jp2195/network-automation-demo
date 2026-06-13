"""Minimal in-cluster Kubernetes API client (stdlib only).

The eventing image deliberately has no kubectl and no kubernetes
package; the only API surface this lane needs is ConfigMap
create/delete in the monitoring namespace, authorized by the pod's
mounted ServiceAccount token (see workloads/observability/
rbac-incident-dashboards.yaml for the grant).
"""

import json
import ssl
import urllib.request

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_API = "https://kubernetes.default.svc"


def _request(method, path, body=None):
    with open(f"{_SA_DIR}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{_SA_DIR}/ca.crt")
    req = urllib.request.Request(
        _API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return r.status, _json_or_empty(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json_or_empty(e.read())


def _json_or_empty(raw):
    # An intermediary (proxy/LB) can return non-JSON error bodies; the
    # (status, body) contract must hold regardless.
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def create_configmap(namespace, name, data, labels, annotations):
    """Create (or replace on 409) a ConfigMap. Raises on other errors."""
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "labels": labels,
                     "annotations": annotations},
        "data": data,
    }
    path = f"/api/v1/namespaces/{namespace}/configmaps"
    status, resp = _request("POST", path, body)
    if status == 409:
        delete_configmap(namespace, name)
        status, resp = _request("POST", path, body)
    if status not in (200, 201):
        raise RuntimeError(f"configmap create failed: {status} {resp}")


def delete_configmap(namespace, name):
    """Delete a ConfigMap; 404 is fine (already gone)."""
    status, resp = _request(
        "DELETE", f"/api/v1/namespaces/{namespace}/configmaps/{name}")
    if status not in (200, 202, 404):
        raise RuntimeError(f"configmap delete failed: {status} {resp}")
