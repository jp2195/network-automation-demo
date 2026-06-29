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


def build_full():
    """The complete bundle — DOM snapshot + per-node interface-state JSON.
    This is large (raw gNMI dumps), so it rides as an ATTACHED FILE, never
    inline in the channel."""
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


def build_summary():
    """The concise, skimmable thread reply. The optical DOM snapshot is small
    and useful at a glance, so it goes inline wrapped in a code fence (Slack
    renders the pipe table as aligned monospace; it does NOT render markdown
    tables). The raw per-node interface-state JSON is deliberately left out of
    the channel — it lives in the attached file."""
    lines = [f"🔍 *Forensic snapshot* — `{AFFECTED}` · {LINK_ID}"]
    if REPORT_DOM and REPORT_DOM.strip():
        lines.append("```\n" + REPORT_DOM.strip() + "\n```")
    lines.append("_Full interface-state bundle attached below._")
    return "\n".join(lines)


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


def post_slack(summary, full, channel=None, thread_ts=None):
    try:
        from slack_sdk import WebClient
    except ImportError:
        print("slack_sdk not installed; skipping", file=sys.stderr, flush=True)
        return False
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    target_channel = channel or os.environ["SLACK_CHANNEL_ID"]

    # Preferred: a tidy summary as the thread reply, with the full raw bundle
    # attached as a file (one click / download, not an inline JSON wall).
    try:
        client.files_upload_v2(
            channel=target_channel, thread_ts=thread_ts,
            filename=f"incident-bundle-{AFFECTED}.md", title="Forensic bundle",
            content=full, initial_comment=summary)
        print(f"posted incident bundle (file) thread_ts={thread_ts}", flush=True)
        return True
    except Exception as e:
        # Falls here if the bot lacks files:write — degrade to the concise
        # summary inline. The raw JSON still never hits the channel.
        print(f"file upload unavailable ({e}); posting summary only",
              file=sys.stderr, flush=True)

    kwargs = {"channel": target_channel, "text": summary, "mrkdwn": True}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    print(f"posted incident bundle (summary) ts={resp['ts']} thread_ts={thread_ts}", flush=True)
    return True


def main():
    summary, full = build_summary(), build_full()
    if slack_unconfigured():
        print("slack not configured — printing bundle to stderr",
              file=sys.stderr, flush=True)
        print(full, file=sys.stderr)
        return
    channel, thread_ts = lookup_thread_ts()
    if not post_slack(summary, full, channel=channel, thread_ts=thread_ts):
        print(full, file=sys.stderr)


if __name__ == "__main__":
    main()
