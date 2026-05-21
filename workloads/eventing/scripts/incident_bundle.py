#!/usr/bin/env python3
"""Stitch the affected-device + neighbor reports into a single
operator-facing forensic bundle. Posts as a Slack thread reply if
the slack-bot Secret + Valkey ledger are populated, otherwise
short-circuits to stderr.
"""

import os
import sys
from datetime import datetime, timezone


def env(name, default=""):
    return os.environ.get(name, default) or default


AFFECTED        = env("AFFECTED", "<unknown>")
LINK_ID         = env("LINK_ID", "<unknown>")
REPORT_AFFECTED = env("REPORT_AFFECTED", "_(no report)_")
REPORT_PEER     = env("REPORT_PEER", "")
REPORT_RING_A   = env("REPORT_RING_A", "")
REPORT_RING_B   = env("REPORT_RING_B", "")
REPORT_DOM      = env("REPORT_DOM", "")


def build_text():
    parts = [
        f"# Incident forensic bundle — {AFFECTED} / {LINK_ID}",
        f"_collected at {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
    ]
    if REPORT_DOM and REPORT_DOM.strip():
        parts.extend([REPORT_DOM, ""])
    parts.append(REPORT_AFFECTED)
    for r in (REPORT_PEER, REPORT_RING_A, REPORT_RING_B):
        if r and r.strip():
            parts.append("")
            parts.append(r)
    return "\n".join(parts)


def slack_unconfigured():
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    chan = os.environ.get("SLACK_CHANNEL_ID", "")
    return not (tok and chan)


def lookup_thread_ts():
    """If the enriched-notify Workflow already posted to Slack for this
    incident, the message ts lives in Valkey at incident:<fingerprint>
    as a JSON blob (see notify.py). Walk incident:* keys, pick the
    most-recent record whose impact mentions this affected node."""
    try:
        import json
        import valkey  # noqa: WPS433 — optional at runtime
    except ImportError:
        return None, None
    url = os.environ.get("VALKEY_URL")
    if not url:
        return None, None
    try:
        r = valkey.from_url(url, decode_responses=True)
        latest = None
        latest_seen = None
        for key in r.scan_iter("incident:*"):
            raw = r.get(key)
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                continue
            impact = record.get("impact") or {}
            affected = impact.get("affected_device", "")
            downstream = [d.get("device") for d in impact.get("downstream_devices", [])]
            if AFFECTED not in (affected, *downstream):
                continue
            seen = record.get("first_seen") or ""
            if latest_seen is None or seen > latest_seen:
                latest_seen = seen
                latest = record
        if latest:
            return latest.get("channel"), latest.get("ts")
    except Exception as e:
        print(f"valkey lookup failed: {e}", file=sys.stderr, flush=True)
    return None, None


def post_slack(text, channel=None, thread_ts=None):
    try:
        from slack_sdk import WebClient
    except ImportError:
        print("slack_sdk not installed; skipping", file=sys.stderr, flush=True)
        return False
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    target_channel = channel or os.environ["SLACK_CHANNEL_ID"]
    # Slack's max message size is 40000 chars; trim if needed.
    payload = text if len(text) < 38000 else text[:38000] + "\n…(truncated)"
    kwargs = {"channel": target_channel, "text": payload, "mrkdwn": True}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    print(f"posted incident bundle ts={resp['ts']} thread_ts={thread_ts}", flush=True)
    return True


def main():
    text = build_text()
    if slack_unconfigured():
        print("slack not configured — printing bundle to stderr",
              file=sys.stderr, flush=True)
        print(text, file=sys.stderr)
        return
    channel, thread_ts = lookup_thread_ts()
    posted = post_slack(text, channel=channel, thread_ts=thread_ts)
    if not posted:
        print(text, file=sys.stderr)


if __name__ == "__main__":
    main()
