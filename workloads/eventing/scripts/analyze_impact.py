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

    # One batched query for every interface attached to the affected
    # device, with cable expanded inline. NetBox 4.x supports `expand`
    # to nest related objects in the response.
    ifc_resp = get(
        "/api/dcim/interfaces/",
        device=affected_device, limit=100, expand="cable",
    )
    downstream = []
    for iface in ifc_resp.get("results", []):
        cable = iface.get("cable") or {}
        if not cable:
            continue
        # Walk both sides of the cable's terminations; collect the
        # device name on the side that is NOT the affected device.
        for side in ("a_terminations", "b_terminations"):
            for t in cable.get(side, []):
                if t.get("object_type") != "dcim.interface":
                    continue
                # NetBox returns the device name inline on expanded
                # terminations under `device.name`. Fall back to the
                # interface name if device name is missing.
                tiface = t.get("object") or {}
                tdev = (tiface.get("device") or {}).get("name", "")
                tname = tiface.get("name", "")
                if tdev and tdev != affected_device:
                    downstream.append({
                        "device": tdev,
                        "interface": tname,
                        "cable_label": cable.get("label"),
                    })

    site_slug = enrichment.get("device", {}).get("site_slug")

    # Batched tenant lookup for affected + downstream devices in one call.
    affected_devices = sorted({affected_device, *(d["device"] for d in downstream)})
    agencies = set()
    if affected_devices:
        names = ",".join(affected_devices)
        dev_resp = get("/api/dcim/devices/", **{"name__in": names})
        for d in dev_resp.get("results", []):
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
