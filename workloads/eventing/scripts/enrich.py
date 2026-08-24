#!/usr/bin/env python3
"""Enrich an Alertmanager webhook payload with NetBox metadata.

Reads ALERT_JSON from env, expects standard Alertmanager webhook v4 payload.
Looks up the affected device + interface + cable in NetBox and writes the
combined enrichment as JSON to stdout (Argo step output).
"""

import json
import os
import sys

from netbox_client import Client


_nb = Client()
get = _nb.get

# Reasons this run produced thinner context than usual, surfaced on the
# incident card via enrichment["degraded"].
_failures = []


def _get(path, **params):
    """NetBox GET that never raises.

    netbox_client raises on any non-2xx, and urllib raises on timeout or
    connection-refused. enrich is the FIRST step of the incident pipeline,
    so an exception here fails the whole Workflow and the incident is never
    announced at all — strictly worse than announcing it with less context.
    A missing *device* was already handled; a NetBox *outage* was not.

    Mirrors the contract prom.py and loki.py already follow (degrade to
    empty, let the caller carry on) rather than being the one lookup on the
    critical path that can take the notification down with it.
    """
    try:
        return get(path, **params) or {}
    except Exception as e:
        msg = f"netbox {path} unavailable ({type(e).__name__})"
        _failures.append(msg)
        print(f"warning: {msg}: {e}", file=sys.stderr)
        return {}


def device_name_from_source(source):
    # source label looks like "atlanta-tmc-1.clabernetes.svc.cluster.local:57400"
    host = source.split(":", 1)[0]
    short = host.split(".", 1)[0]
    return short.split("-", 1)[1] if "-" in short else short


def main():
    alert = json.loads(os.environ["ALERT_JSON"])
    a = alert["alerts"][0]
    labels = a["labels"]

    # Prefer the `node` label (canonical post-ServiceMonitor relabel for
    # gNMIc-derived SRL alerts AND native on dom-synth metrics). Fall back
    # to deriving from the gNMIc `source` FQDN for older alert pipelines.
    device_name = labels.get("node") or device_name_from_source(labels.get("source", ""))
    iface_name = labels.get("interface") or labels.get("interface_name", "")

    if not device_name:
        sys.exit("no node/source label on alert")

    # A device missing from NetBox (stale seed, typo'd label) must not
    # abort the incident pipeline — degrade to a name-only enrichment and
    # let notify still post, flagged as partial.
    degraded = []
    devices = _get("/api/dcim/devices/", name=device_name)
    device = devices["results"][0] if devices.get("results") else None
    if device is None and not _failures:
        degraded.append(f"device {device_name} not found in NetBox")
        print(f"warning: {degraded[0]} — emitting degraded enrichment",
              file=sys.stderr)

    interface, cable, site = {}, {}, {}
    if device:
        interfaces = _get("/api/dcim/interfaces/",
                          device_id=device["id"], name=iface_name)
        interface = interfaces["results"][0] if interfaces.get("results") else {}

        if interface.get("cable"):
            cable = _get(f"/api/dcim/cables/{interface['cable']['id']}/")

        if device.get("site"):
            site = _get(f"/api/dcim/sites/{device['site']['id']}/")

    enrichment = {
        "alert": {
            "name": labels.get("alertname"),
            "severity": labels.get("severity"),
            "corridor": labels.get("corridor"),
            "link_id": labels.get("link_id"),
            "link_kind": labels.get("link_kind"),
            "node": labels.get("node"),
            "interface": labels.get("interface") or iface_name,
            "started": a.get("startsAt"),
            "ended": a.get("endsAt"),
            "status": a.get("status"),
            "fingerprint": a.get("fingerprint"),
        },
        "device": {
            "name": device["name"],
            "role": device.get("role", {}).get("slug") if device.get("role") else None,
            "site": site.get("name"),
            "site_slug": site.get("slug"),
            "lat": site.get("latitude"),
            "lon": site.get("longitude"),
            "primary_ip": device.get("primary_ip4", {}).get("address") if device.get("primary_ip4") else None,
            "custom_fields": device.get("custom_fields", {}),
        } if device else {"name": device_name},
        "interface": {
            "name": interface.get("name"),
            "type": interface.get("type", {}).get("value") if interface.get("type") else None,
            "description": interface.get("description"),
        },
        "cable": {
            "id": cable.get("id"),
            "label": cable.get("label"),
            "status": cable.get("status", {}).get("value") if cable.get("status") else None,
            "custom_fields": cable.get("custom_fields", {}),
            "owner": cable.get("owner") or {},
            "site_group": cable.get("site_group") or {},
            "terminations": [
                {
                    "object_type": t.get("object_type"),
                    "object_id": t.get("object_id"),
                }
                for side in ("a_terminations", "b_terminations")
                for t in cable.get(side, [])
            ],
        } if cable else {},
        "degraded": degraded + _failures,
    }

    json.dump(enrichment, sys.stdout)


if __name__ == "__main__":
    main()
