"""Shared timestamp parsing/formatting for the eventing scripts.

Extracted from notify.py so postmortem.py shares one implementation.
"""

from datetime import datetime


def parse_iso(ts):
    """Parse an ISO-8601 timestamp (Z-suffixed ok); None on absent/bad."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def humanize_seconds(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"
