"""Shared Loki range-query helper for the eventing scripts.

Mirrors prom.py's contract: any failure (network error, non-success
status, malformed body) degrades to [] so a Loki hiccup never fails the
calling workflow step.
"""

import json
import urllib.parse
import urllib.request


def loki_query_range(loki_url, logql, start_unix, end_unix, limit=200, timeout=10):
    """Run a LogQL range query; return [(ts_ns:int, line:str)] sorted
    ascending by timestamp, or [] on any failure.

    direction=backward makes Loki's `limit` keep the most-recent N lines
    in the window; the result is re-sorted ascending before returning."""
    qs = urllib.parse.urlencode({
        "query": logql,
        "start": str(int(start_unix * 1e9)),
        "end": str(int(end_unix * 1e9)),
        "limit": str(limit),
        "direction": "backward",
    })
    url = loki_url.rstrip("/") + "/loki/api/v1/query_range?" + qs
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read())
    except Exception:
        return []
    if body.get("status") != "success":
        return []
    out = []
    for stream in body.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            try:
                out.append((int(ts), line))
            except (TypeError, ValueError):
                continue
    out.sort()
    return out
