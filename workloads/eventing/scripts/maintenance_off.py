"""Close a maintenance window for $NODE: delete the Alertmanager silence(s)
this workflow created and write a NetBox journal entry. Extracted from
wft-maintenance.yaml's maintenance-off inline block."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from constants import MAINTENANCE_CREATED_BY
from netbox_client import Client


NODE = os.environ["NODE"]
AM = os.environ["ALERTMANAGER_URL"].rstrip("/")


def _list_silences():
    with urllib.request.urlopen(AM + "/api/v2/silences", timeout=10) as r:
        return json.loads(r.read())


def _delete_silence(sid):
    req = urllib.request.Request(AM + "/api/v2/silence/" + sid, method="DELETE")
    urllib.request.urlopen(req, timeout=10).read()


def main():
    targets = []
    for s in _list_silences():
        state = (s.get("status", {}) or {}).get("state", "")
        if state != "active" or s.get("createdBy") != MAINTENANCE_CREATED_BY:
            continue
        if any(m.get("name") == "node" and m.get("value") == NODE for m in s.get("matchers", [])):
            targets.append(s["id"])

    for sid in targets:
        _delete_silence(sid)
        print(f"removed silence {sid}", flush=True)
    print(f"silences removed: {len(targets)}", flush=True)

    nb = Client()
    if not nb.base or not nb.token:
        return
    did = nb.find_id("/api/dcim/devices/", name=NODE)
    if not did:
        return
    entry = {
        "assigned_object_type": "dcim.device",
        "assigned_object_id": did,
        "kind": "success",
        "comments": (
            f"**Maintenance window closed** \n\n"
            f"- Closed at: {datetime.now(timezone.utc).isoformat()} \n"
            f"- Silences removed: {len(targets)}\n"
        ),
    }
    try:
        nb.post("/api/extras/journal-entries/", entry)
        print(f"netbox journal entry written for device {NODE}", flush=True)
    except Exception as e:
        print(f"netbox journal entry FAILED: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
