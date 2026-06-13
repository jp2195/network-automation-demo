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

DASHBOARD_NAMESPACE = "incident-dashboards"
FOLDER = "Incidents"

# Stable id for the AI section so the analyst lane can find and replace
# it with a rendered panel once its analysis is ready (inject_analysis).
AI_PANEL_ID = 900

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


def _row(pid, title, y):
    return _panel(pid, title, 0, y, 24, 1, type="row", collapsed=False,
                  panels=[])


def _mappings(pairs):
    # pairs: {"1": ("UP", "green"), ...} → Grafana value-mapping block.
    return [{"type": "value", "options": {
        v: {"text": t, "color": c, "index": i}
        for i, (v, (t, c)) in enumerate(pairs.items())}}]


def _logs_options():
    return {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
            "dedupStrategy": "none", "showCommonLabels": False,
            "showLabels": False, "enableLogDetails": True}


def _stat(pid, title, x, y, w, h, expr, mappings, steps):
    # Value-mapped stat: shows mapped text (e.g. "UP"/"DOWN") with a
    # coloured background, not a bare metric number.
    return _panel(
        pid, title, x, y, w, h, type="stat", datasource=_PROM,
        targets=[{"datasource": _PROM, "expr": expr, "refId": "A",
                  "instant": True}],
        fieldConfig={"defaults": {
            "mappings": mappings,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": steps},
        }, "overrides": []},
        options={"reduceOptions": {"calcs": ["lastNotNull"]},
                 "colorMode": "value", "graphMode": "none",
                 "textMode": "value_and_name", "justifyMode": "center"})


def build_dashboard(enrichment, impact, fp):
    alert = enrichment.get("alert", {})
    device = enrichment.get("device", {}).get("name", "?")
    cable = enrichment.get("cable") or {}
    link_id = alert.get("link_id") or ""
    link_ok = bool(re.fullmatch(r"[A-Za-z0-9_-]+", link_id))

    sev = (impact.get("severity_class") or "?").lower()
    sev_badge = {"high": "🔴 HIGH", "medium": "🟠 MEDIUM",
                 "warning": "🟡 WARNING", "low": "🟢 LOW"}.get(sev, f"⚪ {sev}")
    agencies = impact.get("affected_agencies") or []

    panels, pid, y = [], 1, 0

    # Header — a bold title line over a tidy fact strip. Markdown keeps it
    # dependency-free (no query) while reading like an incident header.
    n_ag = len(agencies)
    ag_str = (f"{n_ag} agenc{'y' if n_ag == 1 else 'ies'} affected"
              if n_ag else "no agency isolation (ring redundancy held)")
    facts = [sev_badge, f"link `{link_id or 'n/a'}`"]
    if cable.get("sla"):
        facts.append(f"SLA {cable['sla']}")
    facts.append(ag_str)
    header = [f"# 🚨 {alert.get('name', 'Incident')} — {device}",
              "&nbsp;&nbsp;·&nbsp;&nbsp;".join(facts)]
    if cable.get("label"):
        header.append(
            f"`{cable['label']}` · {cable.get('provider', '?')} · "
            f"corridor {cable.get('corridor', '?')}"
            + (f" · circuit {cable['circuit_id']}"
               if cable.get("circuit_id") else ""))
    if agencies:
        header.append("**Agencies:** " + ", ".join(agencies))
    panels.append(_panel(pid, "", 0, y, 24, 4, type="text",
                         transparent=True,
                         options={"mode": "markdown",
                                  "content": "\n\n".join(header)}))
    pid += 1
    y += 4

    _OPER = _mappings({"1": ("● UP", "green"), "2": ("● DOWN", "red")})
    _OPER_STEPS = [{"color": "green", "value": None},
                   {"color": "red", "value": 2}]
    _REACH = _mappings({"0": ("● UNREACHABLE", "red"),
                        "1": ("● REACHABLE", "green")})
    _REACH_STEPS = [{"color": "red", "value": None},
                    {"color": "green", "value": 1}]

    if link_ok:
        panels.append(_row(pid, "Link health", y))
        pid += 1
        y += 1
        # state-timeline: coloured UP/DOWN bands over the window — far
        # more legible than a 1/2 line plot.
        panels.append(_panel(
            pid, f"Link state — {link_id}", 0, y, 12, 8,
            type="state-timeline", datasource=_PROM,
            targets=[{"datasource": _PROM, "refId": "A",
                      "expr": f'link:oper_state_with_meta{{link_id="{link_id}"}}',
                      "legendFormat": "{{node}}/{{interface}}"}],
            fieldConfig={"defaults": {"mappings": _OPER,
                                      "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute",
                                                     "steps": _OPER_STEPS}},
                         "overrides": []},
            options={"mergeValues": True, "showValue": "never",
                     "rowHeight": 0.9, "legend": {"displayMode": "list",
                                                  "placement": "bottom"}}))
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
                 "legendFormat": "out {{node}}"}],
            fieldConfig={"defaults": {
                "unit": "bps",
                "custom": {"drawStyle": "line", "lineInterpolation": "smooth",
                           "fillOpacity": 12, "showPoints": "never"}},
                "overrides": []},
            options={"legend": {"displayMode": "list", "placement": "bottom"},
                     "tooltip": {"mode": "multi"}}))
        pid += 1
        y += 8

    # Downstream health grid — did the redundancy hold?
    downstream = impact.get("downstream_devices") or []
    grid = []
    for d in downstream[:8]:
        name = d.get("device") or ""
        iface = d.get("interface") or ""
        if not re.fullmatch(r"[a-z0-9./-]+", name) or \
           not re.fullmatch(r"[A-Za-z0-9./-]+", iface):
            continue
        if name.startswith(CABINET_NAME_PREFIX):
            grid.append((f"{name} · SNMP",
                         f'up{{job="snmp-frr-cabinets", node="{name}"}}',
                         _REACH, _REACH_STEPS))
        else:
            grid.append((f"{name} · {iface}",
                         f'srl_nokia_interfaces_interface_oper_state'
                         f'{{node="{name}", interface="{iface}"}}',
                         _OPER, _OPER_STEPS))
    if grid:
        panels.append(_row(pid, "Downstream health — did the redundancy hold?", y))
        pid += 1
        y += 1
        cols = min(len(grid), 6)
        w = max(4, 24 // cols)
        x = 0
        for title, expr, mp, steps in grid:
            panels.append(_stat(pid, title, x, y, w, 4, expr, mp, steps))
            pid += 1
            x += w
            if x + w > 24:
                x, y = 0, y + 4
        if x:
            y += 4

    sfp = safe_fp(fp)
    panels.append(_row(pid, "Diagnostics", y))
    pid += 1
    y += 1
    # AI section — starts as a placeholder note; the analyst lane
    # replaces this exact panel (by AI_PANEL_ID) with a rendered
    # markdown panel once its analysis is ready (inject_analysis).
    panels.append(_panel(
        AI_PANEL_ID, "AI analyst", 0, y, 24, 6, type="text",
        transparent=True,
        options={"mode": "markdown",
                 "content": "_⏳ AI analyst is investigating — its findings "
                            "will appear here when ready._"}))
    y += 6

    if re.fullmatch(r"[a-z0-9.-]+", device):
        panels.append(_panel(
            pid, f"Device logs — {device}", 0, y, 24, 8,
            type="logs", datasource=_LOKI,
            targets=[{"datasource": _LOKI, "refId": "A",
                      "expr": f'{{namespace="clabernetes"}} |= "{device}"'}],
            options=_logs_options()))

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


def analysis_markdown(analysis):
    """Render an IncidentAnalysis dict as Grafana-panel markdown."""
    conf = analysis.get("confidence")
    conf_str = f" · confidence {conf:.0%}" if isinstance(conf, (int, float)) \
        else ""
    lines = [f"### 🤖 AI analysis{conf_str}", "",
             (analysis.get("summary") or "").strip() or "_no summary_", ""]
    if analysis.get("probable_root_cause"):
        lines += [f"**Probable root cause** — {analysis['probable_root_cause']}",
                  ""]
    if analysis.get("recommendation"):
        lines += [f"**Recommendation** — {analysis['recommendation']}", ""]
    evidence = analysis.get("evidence") or []
    if evidence:
        lines += ["| source | query | observation |", "|---|---|---|"]
        for e in evidence:
            q = str(e.get("query", "")).replace("|", "\\|")
            obs = str(e.get("observation", "")).replace("|", "\\|")
            lines.append(f"| {e.get('source', '')} | `{q}` | {obs} |")
    return "\n".join(lines)


def inject_analysis(fp, analysis):
    """Best-effort: replace the placeholder AI panel in this incident's
    dashboard with the rendered analysis. No-op (returns False) if the
    dashboard ConfigMap isn't present — the lanes stay decoupled, this is
    pure enhancement. Never raises."""
    sfp = safe_fp(fp)
    name = cm_name(sfp)
    try:
        cm = k8s_api.get_configmap(DASHBOARD_NAMESPACE, name)
        if not cm:
            return False
        key = f"{name}.json"
        dash = json.loads(cm["data"][key])
        for p in dash.get("panels", []):
            if p.get("id") == AI_PANEL_ID:
                p["type"] = "text"
                p["options"] = {"mode": "markdown",
                                "content": analysis_markdown(analysis)}
                p["title"] = "AI analyst — IncidentAnalysis"
                break
        else:
            return False
        return k8s_api.patch_configmap_data(
            DASHBOARD_NAMESPACE, name, {key: json.dumps(dash)})
    except Exception as e:
        print(f"incident dashboard analysis injection skipped: {e}",
              file=sys.stderr)
        return False


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
