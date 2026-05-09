#!/usr/bin/env python3
"""Enrich an Alertmanager webhook payload with NetBox metadata.

Reads ALERT_JSON from env, expects standard Alertmanager webhook v4 payload.
Looks up the affected device + interface + cable in NetBox and writes the
combined enrichment as JSON to stdout (Argo step output).
"""

import json
import os
import sys
import urllib.parse
import urllib.request


NETBOX = os.environ["NETBOX_URL"].rstrip("/")
TOKEN = os.environ["NETBOX_TOKEN"]
HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def get(path, **params):
    qs = "?" + urllib.parse.urlencode(params) if params else ""
    req = urllib.request.Request(f"{NETBOX}{path}{qs}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def device_name_from_source(source):
    # source label looks like "atlanta-tmc-1.clabernetes.svc.cluster.local:57400"
    host = source.split(":", 1)[0]
    short = host.split(".", 1)[0]
    return short.split("-", 1)[1] if "-" in short else short


def main():
    alert = json.loads(os.environ["ALERT_JSON"])
    a = alert["alerts"][0]
    labels = a["labels"]

    device_name = device_name_from_source(labels.get("source", ""))
    iface_name = labels.get("interface_name", "")

    devices = get("/api/dcim/devices/", name=device_name)
    if not devices.get("results"):
        sys.exit(f"device {device_name} not found")
    device = devices["results"][0]

    interfaces = get("/api/dcim/interfaces/",
                     device_id=device["id"], name=iface_name)
    interface = interfaces["results"][0] if interfaces.get("results") else {}

    cable = {}
    if interface.get("cable"):
        cables = get(f"/api/dcim/cables/{interface['cable']['id']}/")
        cable = cables

    site = get(f"/api/dcim/sites/{device['site']['id']}/")

    enrichment = {
        "alert": {
            "name": labels.get("alertname"),
            "severity": labels.get("severity"),
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
        },
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
            "terminations": [
                {
                    "object_type": t.get("object_type"),
                    "object_id": t.get("object_id"),
                }
                for side in ("a_terminations", "b_terminations")
                for t in cable.get(side, [])
            ],
        } if cable else {},
    }

    json.dump(enrichment, sys.stdout)


if __name__ == "__main__":
    main()
