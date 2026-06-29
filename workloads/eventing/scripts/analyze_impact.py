#!/usr/bin/env python3
"""Walk the NetBox cable graph from the affected device to estimate impact.

Reads enrichment JSON from $ENRICHMENT_JSON. For the affected device's site
plus any device on the other end of the failed cable, totals up the agencies (device tags)
and the count of cabinet (eBGP) downstream nodes that would lose
their uplink.

Output: JSON {downstream_devices: [...], affected_agencies: [...], severity_class: "..."}.
"""

import json
import os
import sys

from netbox_client import Client
from prom import prom_query
from constants import (
    ROLE_FIELD_CABINET, CABINET_NAME_PREFIX,
    LINK_KIND_BACKBONE, LINK_KIND_CABINET,
    SEVERITY_HIGH, SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_WARNING,
)


def compute_backup_path(alert, affected_role=None):
    """Derive the modeled backup path from the ring topology + LIVE oper-state.

    The contribution here is that the alternate path is not asserted — it is
    derived from the source-of-truth model (which links form the corridor ring)
    and then cross-checked against live telemetry:

      * A backbone (ring) link reroutes via the corridor ring. That backup is
        intact unless ANOTHER backbone ring link is also down right now, in
        which case it is reported as degraded.
      * A cabinet uplink is single-homed — there is no modeled backup, and we
        say so explicitly rather than implying resilience that doesn't exist.

    Returns a dict {available, via, state, detail} for the notification.
    """
    link_kind = alert.get("link_kind")
    link_id = alert.get("link_id")

    # A single-homed field cabinet has no alternate path — say so explicitly;
    # the fragile legacy edge is the point. The SNMP cabinet alert carries no
    # link_kind/link_id labels, so also key off the modeled affected role and
    # the cabinet-specific alertname.
    if (link_kind == LINK_KIND_CABINET
            or affected_role == ROLE_FIELD_CABINET
            or alert.get("name") == "CabinetInterfaceOperDown"):
        return {"available": False, "via": None, "state": "none",
                "detail": "single-homed field cabinet — no alternate path"}
    if link_kind != LINK_KIND_BACKBONE:
        return {"available": None, "via": None, "state": "unknown", "detail": ""}

    prom_url = os.environ.get("PROM_URL")
    other_down = []
    if prom_url:
        rows = prom_query(
            prom_url,
            '(srl_nokia_interfaces_interface_oper_state == 2)'
            ' * on(node, interface) group_left(link_id)'
            ' link_membership_info{link_kind="%s"}' % LINK_KIND_BACKBONE,
        )
        other_down = sorted({
            r.get("metric", {}).get("link_id")
            for r in rows
            if r.get("metric", {}).get("link_id") not in (None, link_id)
        })
    if other_down:
        return {"available": True, "via": "corridor ring", "state": "degraded",
                "detail": "ring also degraded at: " + ", ".join(other_down)}
    return {"available": True, "via": "corridor ring", "state": "up",
            "detail": "corridor ring intact"}


_nb = Client()
get = _nb.get


def main():
    enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
    affected_device = enrichment.get("device", {}).get("name")

    # One call: all cables touching this device. NetBox 4.x's cable
    # endpoint supports the `device=<name>` filter; the `id__in` and
    # `name__in` lookups are silently ignored on this serializer.
    # Each cable's terminations include the peer interface inline
    # (object.device.name, object.name), so we don't need a second fetch.
    cables = get(
        "/api/dcim/cables/", device=affected_device, limit=100,
    ).get("results", [])

    downstream = []
    for cable in cables:
        for side in ("a_terminations", "b_terminations"):
            for t in cable.get(side, []):
                if t.get("object_type") != "dcim.interface":
                    continue
                obj = t.get("object") or {}
                pdev = (obj.get("device") or {}).get("name", "")
                if not pdev or pdev == affected_device:
                    # Skip the affected device's own termination — the
                    # peer is on the OTHER side of the cable.
                    continue
                downstream.append({
                    "device": pdev,
                    "interface": obj.get("name", ""),
                    "cable_label": cable.get("label"),
                })

    site_slug = enrichment.get("device", {}).get("site_slug")

    # One call: tenant lookup for affected + all downstream devices.
    # NetBox accepts repeated `?name=A&name=B&name=C` for OR-filtering;
    # `name__in=A,B,C` is silently ignored. netbox_client.Client encodes
    # a list value as repeated query params via urlencode(doseq=True).
    affected_devices = sorted({affected_device, *(d["device"] for d in downstream)})
    agencies = set()
    roles = {}
    if affected_devices:
        dev_resp = get("/api/dcim/devices/", name=affected_devices, limit=200)
        for d in dev_resp.get("results", []):
            # Modeled role from the source of truth — drives severity below.
            roles[d.get("name")] = (d.get("role") or {}).get("slug")
            # Agencies are device tags (a cabinet serves several); total them.
            for tag in d.get("tags", []):
                if tag.get("slug"):
                    agencies.add(tag["slug"])

    def is_field_cabinet(dev):
        # Decide by the MODELED NetBox role, not the device name. Fall back to
        # the name prefix only when the SoT returned no role (degraded
        # enrichment, e.g. NetBox unreachable) so severity still escalates.
        role = roles.get(dev)
        if role:
            return role == ROLE_FIELD_CABINET
        return dev.startswith(CABINET_NAME_PREFIX)

    alert = enrichment.get("alert", {}) or {}
    alert_severity = alert.get("severity", "")

    severity_class = SEVERITY_LOW
    # A field cabinet is high-impact whether it's the AFFECTED device (its only
    # uplink failed → the cabinet and its agencies go dark) or DOWNSTREAM of the
    # failure. Either way the legacy edge loses connectivity.
    if is_field_cabinet(affected_device) or any(is_field_cabinet(d["device"]) for d in downstream):
        severity_class = SEVERITY_HIGH
    elif len(downstream) > 1:
        severity_class = SEVERITY_MEDIUM
    # An explicit alert-label severity=warning is a degradation signal —
    # honor it as long as the impact analysis didn't already escalate.
    if alert_severity == SEVERITY_WARNING and severity_class in (SEVERITY_LOW, SEVERITY_MEDIUM):
        severity_class = SEVERITY_WARNING

    impact = {
        "affected_device": affected_device,
        "site_slug": site_slug,
        "downstream_devices": downstream,
        "affected_agencies": sorted(a for a in agencies if a),
        "severity_class": severity_class,
        "backup_path": compute_backup_path(alert, roles.get(affected_device)),
    }
    json.dump(impact, sys.stdout)


if __name__ == "__main__":
    main()
