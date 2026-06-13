"""Per-incident Grafana dashboard (wave-3 feature).

On firing, the enriched-notify pipeline calls main() to compose a
dashboard from the SAME enrichment/impact context the notify message
uses, and writes it as a ConfigMap the kps Grafana sidecar picks up
(label grafana_dashboard=1, folder annotation Incidents). On resolve,
the ConfigMap is deleted and the sidecar removes the dashboard.

The AI analysis is NOT raced: a Loki logs panel pinned to the
INCIDENT_ANALYSIS_V1 marker + fingerprint self-populates whenever the
advisory lane finishes.
"""

import json
import os
import re
import sys

import k8s_api
from constants import AI_ANALYSIS_MARKER, CABINET_NAME_PREFIX

DASHBOARD_NAMESPACE = "monitoring"
FOLDER = "Incidents"

_PROM = {"type": "prometheus", "uid": "prometheus"}
_LOKI = {"type": "loki", "uid": "loki"}


def safe_fp(fingerprint):
    return re.sub(r"[^A-Za-z0-9]", "", fingerprint or "").lower()


def cm_name(fp):
    return f"incident-{fp}"


def _panel(pid, title, x, y, w, h, **extra):
    p = {"id": pid, "title": title,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}}
    p.update(extra)
    return p


def _stat(pid, title, x, y, w, h, expr):
    return _panel(
        pid, title, x, y, w, h, type="stat", datasource=_PROM,
        targets=[{"datasource": _PROM, "expr": expr, "refId": "A"}],
        fieldConfig={"defaults": {"thresholds": {"mode": "absolute", "steps": [
            {"color": "green", "value": None},
            {"color": "red", "value": 2}]}}, "overrides": []},
        options={"reduceOptions": {"calcs": ["lastNotNull"]},
                 "colorMode": "background"})


def build_dashboard(enrichment, impact, fp):
    alert = enrichment.get("alert", {})
    device = enrichment.get("device", {}).get("name", "?")
    cable = enrichment.get("cable") or {}
    link_id = alert.get("link_id") or ""
    link_ok = bool(re.fullmatch(r"[A-Za-z0-9_-]+", link_id))

    panels, pid, y = [], 1, 0

    ctx_lines = [f"**Alert** {alert.get('name', '?')} on **{device}** "
                 f"(severity class: {impact.get('severity_class', '?')})"]
    if cable.get("label"):
        ctx_lines.append(
            f"**Cable** `{cable['label']}` — {cable.get('provider', '?')}, "
            f"corridor {cable.get('corridor', '?')}, SLA "
            f"{cable.get('sla', '?')}, circuit "
            f"{cable.get('circuit_id', '?')}")
    agencies = impact.get("affected_agencies") or []
    ctx_lines.append("**Agencies affected:** "
                     + (", ".join(agencies) if agencies else "none"))
    panels.append(_panel(pid, "Incident context", 0, y, 24, 4,
                         type="text", options={"mode": "markdown",
                                               "content": "\n\n".join(ctx_lines)}))
    pid += 1
    y += 4

    if link_ok:
        panels.append(_panel(
            pid, f"Link state — {link_id}", 0, y, 12, 8,
            type="timeseries", datasource=_PROM,
            targets=[{"datasource": _PROM, "refId": "A",
                      "expr": f'link:oper_state_with_meta{{link_id="{link_id}"}}',
                      "legendFormat": "{{node}}/{{interface}}"}]))
        pid += 1
        panels.append(_panel(
            pid, f"Link traffic — {link_id}", 12, y, 12, 8,
            type="timeseries", datasource=_PROM,
            targets=[
                {"datasource": _PROM, "refId": "A",
                 "expr": f'link:rate_in_bps:1m{{link_id="{link_id}"}}',
                 "legendFormat": "in {{node}}"},
                {"datasource": _PROM, "refId": "B",
                 "expr": f'link:rate_out_bps:1m{{link_id="{link_id}"}}',
                 "legendFormat": "out {{node}}"}]))
        pid += 1
        y += 8

    # Downstream health grid — did the redundancy hold?
    downstream = impact.get("downstream_devices") or []
    x = 0
    for d in downstream[:8]:
        name = d.get("device") or ""
        iface = d.get("interface") or ""
        if not re.fullmatch(r"[a-z0-9./-]+", name) or \
           not re.fullmatch(r"[A-Za-z0-9./-]+", iface):
            continue
        if name.startswith(CABINET_NAME_PREFIX):
            expr = f'up{{job="snmp-frr-cabinets", node="{name}"}}'
            # up: 1 = reachable (green), 0 = dark — invert threshold
            p = _stat(pid, f"{name} (SNMP reach)", x, y, 6, 4, expr)
            p["fieldConfig"]["defaults"]["thresholds"]["steps"] = [
                {"color": "red", "value": None},
                {"color": "green", "value": 1}]
        else:
            expr = (f'srl_nokia_interfaces_interface_oper_state'
                    f'{{node="{name}", interface="{iface}"}}')
            p = _stat(pid, f"{name} {iface}", x, y, 6, 4, expr)
        panels.append(p)
        pid += 1
        x += 6
        if x >= 24:
            x, y = 0, y + 4
    if x:
        y += 4

    sfp = safe_fp(fp)
    panels.append(_panel(
        pid, "AI analyst — IncidentAnalysis (appears when ready)",
        0, y, 24, 8, type="logs", datasource=_LOKI,
        targets=[{"datasource": _LOKI, "refId": "A",
                  "expr": (f'{{namespace="argo-events"}} '
                           f'|= "{AI_ANALYSIS_MARKER} {{" |= "{sfp}"')}],
        options={"showTime": True, "wrapLogMessage": True,
                 "sortOrder": "Descending", "dedupStrategy": "none",
                 "showCommonLabels": False, "showLabels": False}))
    pid += 1
    y += 8

    if re.fullmatch(r"[a-z0-9.-]+", device):
        panels.append(_panel(
            pid, f"Device logs — {device}", 0, y, 24, 8,
            type="logs", datasource=_LOKI,
            targets=[{"datasource": _LOKI, "refId": "A",
                      "expr": f'{{namespace="clabernetes"}} |= "{device}"'}],
            options={"showTime": True, "wrapLogMessage": True,
                     "sortOrder": "Descending", "dedupStrategy": "none",
                     "showCommonLabels": False, "showLabels": False}))

    return {
        "uid": f"incident-{sfp}",
        "title": (f"INCIDENT — {alert.get('name', '?')} on {device} "
                  f"({sfp[:8]})"),
        "tags": ["incident", "atlas-dot", "generated"],
        "timezone": "browser",
        "schemaVersion": 38,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
        "editable": True,
        "annotations": {"list": []},
        "templating": {"list": []},
        "links": [],
    }


def main():
    # The whole body is advisory: a malformed env var (or any other
    # failure) must never fail the deterministic pipeline, so even the
    # JSON parse lives inside the guard.
    try:
        enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
        impact = json.loads(os.environ["IMPACT_JSON"])
        alert = enrichment.get("alert", {})
        fp = safe_fp(alert.get("fingerprint"))
        if not fp:
            print("no usable fingerprint — skipping incident dashboard")
            return
        name = cm_name(fp)
        if alert.get("status", "firing") == "firing":
            dash = build_dashboard(enrichment, impact, fp)
            k8s_api.create_configmap(
                DASHBOARD_NAMESPACE, name,
                data={f"{name}.json": json.dumps(dash)},
                labels={"grafana_dashboard": "1"},
                annotations={"grafana_folder": FOLDER})
            print(f"incident dashboard created: {DASHBOARD_NAMESPACE}/{name} "
                  f"(uid incident-{fp})")
        else:
            k8s_api.delete_configmap(DASHBOARD_NAMESPACE, name)
            print(f"incident dashboard removed: {DASHBOARD_NAMESPACE}/{name}")
    except Exception as e:
        # Advisory surface — never fail the deterministic pipeline.
        print(f"incident dashboard step failed (non-fatal): {e}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
