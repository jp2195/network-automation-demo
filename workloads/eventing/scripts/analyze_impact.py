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
    if affected_devices:
        dev_resp = get("/api/dcim/devices/", name=affected_devices, limit=200)
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
