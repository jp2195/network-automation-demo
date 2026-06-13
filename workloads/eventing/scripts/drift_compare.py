"""Compare live SR Linux config against the rendered intent and raise
ConfigDrift alerts for any divergence.

Inputs (env):
  LIVE_<NODE>        gnmic `get --type config -e json_ietf` output per node
                     (env suffix = node name uppercased, '-' -> '_');
                     '[]' / empty / non-JSON means the node was unreachable
  EXPECTED_PATH      audited intent JSON (default /scripts/drift_expected.json,
                     emitted by tools/render from spec/atlanta.yaml)
  ALERTMANAGER_URL   Alertmanager base URL (v2 API)
  VALKEY_URL         optional — enables suppression of IS-IS metric drift on
                     links with an active platform remediation claim

Each drift becomes one alert with labels {alertname: ConfigDrift,
severity: warning, namespace: monitoring, node, interface, link_id,
drift}. namespace=monitoring is load-bearing: the AlertmanagerConfig
sub-route matches on it (OnNamespace strategy). endsAt is now+12m so the
alert self-resolves roughly two audit cycles after the drift clears.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from constants import REMEDIATION_ACTIVE_PREFIX

ALERT_TTL_MINUTES = 12


def env_name(node):
    return "LIVE_" + node.replace("-", "_").upper()


def parse_live(raw):
    """gnmic output -> {"interfaces": {name: admin_state},
    "isis": {subif: metric_present}} or None when unreachable."""
    try:
        notifications = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not notifications:
        return None
    out = {"interfaces": {}, "isis": {}}

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("name"), str) and "admin-state" in obj:
                out["interfaces"][obj["name"]] = obj.get("admin-state", "")
            if "interface-name" in obj:
                levels = obj.get("level") or []
                has_metric = any(isinstance(l, dict) and "metric" in l
                                 for l in levels)
                out["isis"][obj["interface-name"]] = has_metric
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(notifications)
    return out


def diff_node(node, expected, live):
    drifts = []
    for ifname, meta in sorted(expected.get("interfaces", {}).items()):
        state = live["interfaces"].get(ifname)
        link = meta.get("link_id", "")
        if state is None:
            drifts.append({"node": node, "interface": ifname, "link_id": link,
                           "kind": "missing-interface",
                           "detail": f"{ifname} absent from running config "
                                     f"(rendered config defines it)"})
        elif state != "enable":
            drifts.append({"node": node, "interface": ifname, "link_id": link,
                           "kind": "admin-state",
                           "detail": f"{ifname} admin-state {state} "
                                     f"(rendered config says enable)"})
    for sub in expected.get("isis_interfaces", []):
        if live["isis"].get(sub):
            base = sub[:-2] if sub.endswith(".0") else sub
            link = expected.get("interfaces", {}).get(base, {}).get("link_id", "")
            drifts.append({"node": node, "interface": sub, "link_id": link,
                           "kind": "isis-metric",
                           "detail": f"IS-IS metric override present on {sub} "
                                     f"(rendered config sets none)"})
    return drifts


def suppress_remediated(drifts, vk):
    """Platform-driven metric raises (feature 1) are not unauthorized
    drift — drop isis-metric findings on links with an active claim."""
    kept, suppressed = [], []
    for d in drifts:
        if d["kind"] == "isis-metric" and d["link_id"] and vk is not None:
            try:
                if vk.exists(REMEDIATION_ACTIVE_PREFIX + d["link_id"]):
                    suppressed.append(d)
                    continue
            except Exception as e:
                print(f"valkey check failed: {e}", file=sys.stderr, flush=True)
        kept.append(d)
    return kept, suppressed


def build_alerts(drifts, now_fn=None):
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    ends = (now + timedelta(minutes=ALERT_TTL_MINUTES)).isoformat()
    alerts = []
    for d in drifts:
        alerts.append({
            "labels": {
                "alertname": "ConfigDrift",
                "severity": "warning",
                "namespace": "monitoring",
                "node": d["node"],
                "interface": d["interface"],
                "link_id": d["link_id"],
                "drift": d["kind"],
            },
            "annotations": {
                "summary": f"Config drift on {d['node']} {d['interface']}",
                "description": f"{d['detail']} — out-of-band change vs the "
                               f"rendered SSOT (spec/atlanta.yaml).",
            },
            # Explicit startsAt: Alertmanager defaults a missing
            # startsAt to endsAt, which dates the incident 12 minutes
            # in the future — wrecking the ledger first_seen, the SLA
            # math, and the postmortem's Loki window (smoke-found).
            "startsAt": now.isoformat(),
            "endsAt": ends,
        })
    return alerts


def post_alerts(am_url, alerts):
    req = urllib.request.Request(
        am_url.rstrip("/") + "/api/v2/alerts",
        data=json.dumps(alerts).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def main():
    with open(os.environ.get("EXPECTED_PATH", "/scripts/drift_expected.json")) as f:
        expected = json.load(f)

    vk = None
    try:
        import valkey
        vk = valkey.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    except Exception as e:
        print(f"valkey unavailable ({e}); remediation suppression disabled",
              file=sys.stderr, flush=True)

    all_drifts, unreachable = [], []
    for node, exp in sorted(expected.items()):
        live = parse_live(os.environ.get(env_name(node), ""))
        if live is None:
            unreachable.append(node)
            continue
        all_drifts.extend(diff_node(node, exp, live))

    kept, suppressed = suppress_remediated(all_drifts, vk)
    for d in suppressed:
        print(f"suppressed (platform remediation active): {d['node']} {d['detail']}",
              flush=True)
    for n in unreachable:
        print(f"unreachable: {n} — skipped this cycle", file=sys.stderr, flush=True)

    if not kept:
        print(f"no drift across {len(expected) - len(unreachable)} nodes", flush=True)
        return

    for d in kept:
        print(f"DRIFT {d['node']} {d['interface']}: {d['detail']}", flush=True)
    status = post_alerts(os.environ["ALERTMANAGER_URL"], build_alerts(kept))
    print(f"posted {len(kept)} ConfigDrift alert(s) (HTTP {status})", flush=True)


if __name__ == "__main__":
    main()
