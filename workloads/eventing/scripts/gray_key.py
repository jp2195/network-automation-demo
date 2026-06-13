#!/usr/bin/env python3
"""Write or clear a gray-failure key in Valkey for the dom-synth exporter.

Event-driven equivalent of bin/scenarios.sh::gray_failure. The scenario
console POSTs a gray-failure event; sensor-gray-failure turns it into a
Workflow that runs this script on the eventing-py image — keeping the
Valkey write inside a Workflow, not in the privilege-free console.

Env:
  LINK          — link_id (e.g. ring-e-i20e); required
  ACTION        — start | end (default start)
  DURATION_S    — optional, default 180  (matches bin/scenarios.sh)
  RX_OFFSET_DBM — optional, default 8.0
  ERR_RATE      — optional, default 120
  VALKEY_URL    — valkey://valkey.valkey.svc.cluster.local:6379/3 (DB 3)
"""

import json
import os
import re
import sys
import time

_LINK_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_link(link):
    if not _LINK_RE.fullmatch(link or ""):
        raise ValueError(f"invalid link id: {link!r}")
    return link


def apply(vk, link, action, duration=180, rx_offset=8.0, err_rate=120,
          now=None):
    """Apply the gray-failure state. Returns (key, payload|None)."""
    key = f"gray:{validate_link(link)}"
    if action == "end":
        vk.delete(key)
        return key, None
    payload = {
        "start_ts": int(now if now is not None else time.time()),
        "duration_s": int(duration),
        "peak_rx_offset_dbm": float(rx_offset),
        "peak_errors_per_sec": int(err_rate),
    }
    vk.set(key, json.dumps(payload), ex=int(duration) + 30)
    return key, payload


def main():
    link = os.environ.get("LINK", "")
    action = os.environ.get("ACTION", "start")
    try:
        validate_link(link)
    except ValueError as e:
        sys.exit(str(e))
    import valkey
    vk = valkey.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    key, payload = apply(
        vk, link, action,
        duration=int(os.environ.get("DURATION_S", "180")),
        rx_offset=float(os.environ.get("RX_OFFSET_DBM", "8.0")),
        err_rate=int(os.environ.get("ERR_RATE", "120")))
    if payload is None:
        print(f"cleared {key}")
    else:
        print(f"set {key} ttl={payload['duration_s'] + 30}")


if __name__ == "__main__":
    main()
