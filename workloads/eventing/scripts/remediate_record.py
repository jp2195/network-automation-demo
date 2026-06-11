"""Finalize one remediation action and tell the operator about it.

cost-out: the decide step already holds remediation:active:<link>; we
          only announce what was done.
restore:  clear the claim (the gNMI deletes succeeded if we are
          running — the DAG gates record on Set success), then announce.

Announcement goes to Slack as a thread reply on the incident message
when the slack-bot Secret + ledger entry exist, else to stderr — same
degrade pattern as notify.py / incident_bundle.py.
"""

import os
import sys

REMEDIATION_METRIC = "16777214"


def build_text(action, link_id, node_a, iface_a, node_b, iface_b):
    ends = f"{node_a}/{iface_a} and {node_b}/{iface_b}"
    if action == "cost-out":
        return (
            f":construction: *Auto-remediation applied* — link `{link_id}` costed out of "
            f"the IS-IS topology (metric {REMEDIATION_METRIC} on {ends}). Traffic is "
            f"shifting to the alternate ring path; the degraded span can be serviced "
            f"without customer impact."
        )
    return (
        f":white_check_mark: *Auto-remediation restored* — IS-IS metric on {ends} "
        f"returned to its rendered value; link `{link_id}` is back in the forwarding path."
    )


def finalize(action, link_id, valkey_url):
    """Bring the Valkey claim in line with the action just performed."""
    if action != "restore" or not valkey_url:
        return
    try:
        import valkey

        from constants import REMEDIATION_ACTIVE_PREFIX

        r = valkey.from_url(valkey_url, decode_responses=True)
        r.delete(REMEDIATION_ACTIVE_PREFIX + link_id)
    except Exception as e:  # state cleanup must never fail the workflow
        print(f"valkey claim cleanup failed: {e}", file=sys.stderr, flush=True)


def lookup_thread_ts(link_id, valkey_url):
    """Find the Slack thread of the incident this remediation belongs to:
    newest incident:<fp> ledger record is close enough for the demo."""
    try:
        import json

        import valkey
    except ImportError:
        return None, None
    if not valkey_url:
        return None, None
    try:
        r = valkey.from_url(valkey_url, decode_responses=True)
        latest, latest_seen = None, None
        for key in r.scan_iter("incident:*"):
            raw = r.get(key)
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                continue
            seen = record.get("first_seen") or ""
            if latest_seen is None or seen > latest_seen:
                latest_seen, latest = seen, record
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
    kwargs = {
        "channel": channel or os.environ["SLACK_CHANNEL_ID"],
        "text": text,
        "mrkdwn": True,
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    print(f"posted remediation note ts={resp['ts']}", flush=True)
    return True


def main():
    action = os.environ.get("ACTION", "")
    link_id = os.environ.get("LINK_ID", "")
    valkey_url = os.environ.get("VALKEY_URL", "")
    text = build_text(
        action, link_id,
        os.environ.get("NODE_A", ""), os.environ.get("IFACE_A", ""),
        os.environ.get("NODE_B", ""), os.environ.get("IFACE_B", ""),
    )
    finalize(action, link_id, valkey_url)

    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    chan = os.environ.get("SLACK_CHANNEL_ID", "")
    if not (tok and chan):
        print("slack not configured — printing remediation note to stderr",
              file=sys.stderr, flush=True)
        print(text, file=sys.stderr)
        return
    channel, thread_ts = lookup_thread_ts(link_id, valkey_url)
    if not post_slack(text, channel=channel, thread_ts=thread_ts):
        print(text, file=sys.stderr)


if __name__ == "__main__":
    main()
