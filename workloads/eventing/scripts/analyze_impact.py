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

    # Step 1: all interfaces on the affected device.
    ifc_resp = get("/api/dcim/interfaces/", device=affected_device, limit=100)
    interfaces = ifc_resp.get("results", [])

    # Step 2: collect cable IDs from interfaces that have one, batch-fetch.
    cable_ids = sorted({
        iface["cable"]["id"]
        for iface in interfaces
        if iface.get("cable") and iface["cable"].get("id")
    })
    cables_by_id = {}
    if cable_ids:
        ids_csv = ",".join(str(i) for i in cable_ids)
        cables_resp = get("/api/dcim/cables/", **{"id__in": ids_csv}, limit=100)
        cables_by_id = {c["id"]: c for c in cables_resp.get("results", [])}

    # Step 3: collect peer-interface IDs (the side of each cable opposite
    # the affected device) and batch-fetch them. Cable terminations are
    # always (object_type, object_id) pairs; we filter for interfaces.
    peer_iface_ids = set()
    cable_for_iface_id = {}  # peer interface id -> cable.label
    for cable in cables_by_id.values():
        for side in ("a_terminations", "b_terminations"):
            for t in cable.get(side, []):
                if t.get("object_type") != "dcim.interface":
                    continue
                pid = t.get("object_id")
                if pid is None:
                    continue
                # We'll filter out interfaces belonging to the affected
                # device in step 4 once we have the device field.
                peer_iface_ids.add(pid)
                cable_for_iface_id[pid] = cable.get("label")

    peer_ifaces = []
    if peer_iface_ids:
        ids_csv = ",".join(str(i) for i in sorted(peer_iface_ids))
        peer_resp = get("/api/dcim/interfaces/", **{"id__in": ids_csv}, limit=200)
        peer_ifaces = peer_resp.get("results", [])

    downstream = []
    for piface in peer_ifaces:
        pdev = (piface.get("device") or {}).get("name", "")
        if not pdev or pdev == affected_device:
            continue
        downstream.append({
            "device": pdev,
            "interface": piface.get("name", ""),
            "cable_label": cable_for_iface_id.get(piface.get("id")),
        })

    site_slug = enrichment.get("device", {}).get("site_slug")

    # Step 5: batched tenant lookup for affected + downstream devices.
    affected_devices = sorted({affected_device, *(d["device"] for d in downstream)})
    agencies = set()
    if affected_devices:
        names_csv = ",".join(affected_devices)
        dev_resp = get("/api/dcim/devices/", **{"name__in": names_csv})
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
