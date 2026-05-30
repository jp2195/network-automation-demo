"""Shared Prometheus instant-query helper for the eventing scripts.

Both identify_targets.py and gather_dom.py previously inlined an identical
urllib query() with no status check, so a non-200 response, an error-status
body, or a missing data key raised HTTPError/KeyError and aborted the
collector. prom_query centralizes it and degrades to [] on any failure.
"""

import json
import urllib.parse
import urllib.request


def prom_query(prom_url, expr, timeout=10):
    """Run an instant query; return the data.result list, or [] on any
    failure (network error, non-success status, malformed body)."""
    url = prom_url.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(expr)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read())
    except Exception:
        return []
    if body.get("status") != "success":
        return []
    return body.get("data", {}).get("result", [])
