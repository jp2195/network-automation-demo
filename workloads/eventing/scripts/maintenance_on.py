"""Open a maintenance window for $NODE: post an Alertmanager silence and
write a NetBox journal entry. Extracted from wft-maintenance.yaml's
maintenance-on inline block; mounted under /scripts and run via
runpy.run_path."""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

from netbox_client import Client


NODE = os.environ["NODE"]
HOURS = float(os.environ["DURATION_HOURS"])
COMMENT = os.environ.get("COMMENT", "scheduled maintenance")
AM = os.environ["ALERTMANAGER_URL"].rstrip("/")


def main():
    now = datetime.now(timezone.utc)
    ends = now + timedelta(hours=HOURS)
    silence = {
        "matchers": [
            {"name": "node", "value": NODE, "isRegex": False, "isEqual": True}
        ],
        "startsAt": now.replace(microsecond=0).isoformat(),
        "endsAt":   ends.replace(microsecond=0).isoformat(),
        "createdBy": "atlas-maintenance",
        "comment": f"{COMMENT} (workflow={os.environ.get('ARGO_WORKFLOW_NAME','?')})",
    }
    req = urllib.request.Request(
        AM + "/api/v2/silences", method="POST",
        data=json.dumps(silence).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
        sid = body.get("silenceID") or body.get("id")
        print(f"silence id: {sid}", flush=True)
        print(f"endsAt:     {ends.isoformat()}", flush=True)

    nb = Client()
    if not nb.base or not nb.token:
        print("netbox: no token / URL — skipping journal entry", flush=True)
        return
    did = nb.find_id("/api/dcim/devices/", name=NODE)
    if not did:
        print(f"netbox: device {NODE} not found, skipping journal entry", flush=True)
        return
    entry = {
        "assigned_object_type": "dcim.device",
        "assigned_object_id": did,
        "kind": "info",
        "comments": (
            f"**Maintenance window opened** \n\n"
            f"- Duration: {HOURS}h \n"
            f"- Until: {ends.isoformat()}Z \n"
            f"- Comment: {COMMENT} \n"
            f"- Alertmanager silence id: {sid}\n"
        ),
    }
    try:
        nb.post("/api/extras/journal-entries/", entry)
        print(f"netbox journal entry written for device {NODE} (id={did})", flush=True)
    except Exception as e:
        print(f"netbox journal entry FAILED: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
