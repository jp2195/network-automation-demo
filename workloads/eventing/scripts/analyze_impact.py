#!/usr/bin/env python3
"""Walk the NetBox cable graph from the affected device to estimate impact.

Reads enrichment JSON from $ENRICHMENT_JSON. For the affected device's site
plus any device on the other end of the failed cable, totals up the tenants
(agencies) and the count of cabinet (eBGP) downstream nodes that would lose
their uplink.

Output: JSON {downstream_devices: [...], affected_agencies: [...], severity_class: "..."}.
"""

import json
import os
import sys

from netbox_client import Client
from constants import (
    CABINET_NAME_PREFIX,
    SEVERITY_HIGH, SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_WARNING,
)


_nb = Client()
get = _nb.get


def main():
    enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
    affected_device = enrichment.get("device", {}).get("name")

    # Cabinets attached to the same device (i.e. devices we eBGP to)
    interfaces = get("/api/dcim/interfaces/", device=affected_device, limit=100)
    downstream = []
    for iface in interfaces.get("results", []):
        cable_ref = iface.get("cable")
        if not cable_ref:
            continue
        cable = get(f"/api/dcim/cables/{cable_ref['id']}/")
        # other end of this cable (device whose name != affected_device)
        for side in ("a_terminations", "b_terminations"):
            for t in cable.get(side, []):
                if t.get("object_type") != "dcim.interface":
                    continue
                tiface = get(f"/api/dcim/interfaces/{t['object_id']}/")
                tdev = tiface.get("device", {}).get("name")
                if tdev and tdev != affected_device:
                    downstream.append({
                        "device": tdev,
                        "interface": tiface.get("name"),
                        "cable_label": cable.get("label"),
                    })

    # Agencies = tenants whose site contains affected_device or any downstream
    site_slug = enrichment.get("device", {}).get("site_slug")
    affected_devices = {affected_device, *(d["device"] for d in downstream)}
    agencies = set()
    for name in affected_devices:
        devs = get("/api/dcim/devices/", name=name)
        for d in devs.get("results", []):
            tenant = d.get("tenant")
            if tenant:
                agencies.add(tenant.get("slug"))

    alert = enrichment.get("alert", {}) or {}
    alert_severity = alert.get("severity", "")

    severity_class = SEVERITY_LOW
    if any(d["device"].startswith(CABINET_NAME_PREFIX) for d in downstream):
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
    }
    json.dump(impact, sys.stdout)


if __name__ == "__main__":
    main()
