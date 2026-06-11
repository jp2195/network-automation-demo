"""Decide what the remediate-link Workflow should do for one alert event.

Reads the Alertmanager webhook body (ALERT_JSON), remediation mode +
claim state from Valkey, and link membership from Prometheus, then
emits Argo output parameters that gate the gNMI Set steps:

  action   cost-out | restore | skip
  node_a/iface_a, node_b/iface_b   both ends of the link, as IS-IS
           subinterface names (ethernet-1/N.0)

All logic lives in decide() with injected dependencies (valkey client,
prom function, sleep) so the unit tests run on fakeredis with no
network. The claim (remediation:active:<link>) is taken here with SET
NX so concurrent workflows for the same link cannot double-fire; the
record step clears it after a successful restore.
"""

import json
import os
import sys
import time

from constants import (
    LINK_KIND_BACKBONE,
    REMEDIATION_ACTIVE_PREFIX,
    REMEDIATION_APPROVE_PREFIX,
    REMEDIATION_MODE_AUTO,
    REMEDIATION_MODE_GATED,
    REMEDIATION_MODE_KEY,
)
from prom import prom_query

OUTPUT_KEYS = ("action", "link_id", "node_a", "iface_a", "node_b", "iface_b")


def _skip(reason):
    return {"action": "skip", "reason": reason, "link_id": "",
            "node_a": "", "iface_a": "", "node_b": "", "iface_b": ""}


def decide(alert_body, vk, prom_fn, prom_url,
           gate_timeout=600, poll_interval=5, sleep_fn=time.sleep):
    alerts = alert_body.get("alerts") or [{}]
    a = alerts[0]
    labels = a.get("labels", {})
    link_id = labels.get("link_id", "")
    status = a.get("status", "firing")

    if not link_id:
        return _skip("alert carries no link_id label")
    if labels.get("link_kind", LINK_KIND_BACKBONE) != LINK_KIND_BACKBONE:
        return _skip(f"{link_id} is not a backbone link; nothing to cost out")

    rows = prom_fn(
        prom_url,
        f'link_membership_info{{link_id="{link_id}", link_kind="{LINK_KIND_BACKBONE}"}}',
    )
    members = sorted(
        (r["metric"]["node"], r["metric"]["interface"])
        for r in rows
        if r.get("metric", {}).get("node") and r.get("metric", {}).get("interface")
    )
    if len(members) != 2:
        return _skip(f"expected 2 members for {link_id}, found {len(members)}")
    (node_a, intf_a), (node_b, intf_b) = members

    active_key = REMEDIATION_ACTIVE_PREFIX + link_id
    result = {
        "action": "", "reason": "", "link_id": link_id,
        "node_a": node_a, "iface_a": intf_a + ".0",
        "node_b": node_b, "iface_b": intf_b + ".0",
    }

    if status == "resolved":
        if not vk.exists(active_key):
            return _skip(f"no active remediation for {link_id}")
        still_firing = prom_fn(
            prom_url,
            f'ALERTS{{alertstate="firing", severity="warning", link_id="{link_id}"}}',
        )
        if still_firing:
            return _skip(f"{link_id} still has firing warning alerts; holding the cost-out")
        result.update(action="restore", reason="all warning alerts resolved")
        return result

    # firing
    if vk.exists(active_key):
        return _skip(f"{link_id} already remediated")

    mode = vk.get(REMEDIATION_MODE_KEY) or REMEDIATION_MODE_AUTO
    if mode == REMEDIATION_MODE_GATED:
        approve_key = REMEDIATION_APPROVE_PREFIX + link_id
        waited = 0
        while True:
            if vk.get(approve_key):
                vk.delete(approve_key)
                break
            if waited >= gate_timeout:
                return _skip(
                    f"gated mode: no approval for {link_id} within {gate_timeout}s "
                    f"(make remediation-approve LINK={link_id})"
                )
            sleep_fn(poll_interval)
            waited += poll_interval

    claimed = vk.set(active_key, json.dumps({"link_id": link_id, "mode": mode}),
                     nx=True, ex=7200)
    if not claimed:
        return _skip(f"{link_id} already remediated (lost claim race)")
    result.update(action="cost-out", reason=f"mode={mode}")
    return result


def main():
    import valkey

    alert_body = json.loads(os.environ["ALERT_JSON"])
    vk = valkey.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    prom_url = os.environ["PROM_URL"].rstrip("/")
    gate_timeout = int(os.environ.get("REMEDIATION_GATE_TIMEOUT", "600"))

    out = decide(alert_body, vk, prom_query, prom_url, gate_timeout=gate_timeout)

    # Output-parameter files; a partial write would feed the gNMI steps
    # empty targets, so fail loudly (same contract as identify_targets).
    try:
        os.makedirs("/tmp/argo", exist_ok=True)
        for k in OUTPUT_KEYS:
            with open(f"/tmp/argo/{k}", "w") as f:
                f.write(out[k])
    except OSError as e:
        sys.exit(f"failed to write Argo output parameters under /tmp/argo: {e}")
    print(f"action={out['action']} link={out['link_id']} reason={out['reason']}",
          flush=True)


if __name__ == "__main__":
    main()
