#!/usr/bin/env python3
"""Post a Slack alert message and reconcile firing↔resolved transitions.

Inputs (env):
  ENRICHMENT_JSON, IMPACT_JSON  — outputs from prior WFT steps
  SLACK_BOT_TOKEN, SLACK_CHANNEL_ID  — bot creds (xoxb-...)
  VALKEY_URL  — incident ledger, e.g. valkey://valkey.valkey.svc.cluster.local:6379/2

Behavior:
  - On firing: chat.postMessage with Block Kit, persist
    {ts, channel, first_seen, impact} in Valkey under
    incident:<fingerprint> with 24h TTL.
  - On resolved: load ledger, chat.update the original message in place
    (✅ header + downtime), then chat.postMessage a thread reply with the
    resolution summary.

If SLACK_BOT_TOKEN or SLACK_CHANNEL_ID is empty (the Slack `slack-bot`
Secret hasn't been created), the script short-circuits to stderr-printed
payloads — the demo runs without real Slack credentials. See SECRETS.md
for how to opt into real Slack posting.
"""

import json
import os
import sys
from datetime import datetime, timezone

from constants import SEVERITY_HIGH, SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_WARNING


def _slack_unconfigured(token, channel):
    return not token or not channel


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _humanize_seconds(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"


def _firing_blocks(enrichment, impact):
    alert = enrichment.get("alert", {})
    device = enrichment.get("device", {})
    iface = enrichment.get("interface", {})
    cable = enrichment.get("cable", {})
    cf = cable.get("custom_fields", {}) if cable else {}
    severity = impact.get("severity_class", SEVERITY_LOW)
    emoji = {
        SEVERITY_HIGH:    "🚨",
        SEVERITY_MEDIUM:  "⚠️",
        SEVERITY_WARNING: "⚠️",
        SEVERITY_LOW:     "ℹ️",
    }.get(severity, "ℹ️")

    # Provider lives on cable.owner (NetBox 4.x owner model). Corridor flows
    # through the alert's relabeled `corridor` label, lifted by the
    # link_membership_info join — fall back to cable.site_group.slug and
    # cable.custom_fields.corridor for back-compat with seed data that still
    # carries it there.
    provider = (cable.get("owner") or {}).get("name") or cf.get("provider") or "unknown provider"
    corridor = (alert.get("corridor")
                or (cable.get("site_group") or {}).get("slug")
                or cf.get("corridor")
                or "unknown")
    sla = cf.get("restoration_sla_hours", "?")

    blocks = [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"{emoji} {alert.get('name', 'Alert')} on {device.get('name')}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Device*\n{device.get('name')}"},
            {"type": "mrkdwn", "text": f"*Interface*\n{iface.get('name')}"},
            {"type": "mrkdwn", "text": f"*Site*\n{device.get('site')}"},
            {"type": "mrkdwn", "text": f"*Severity*\n{severity}"},
        ]},
    ]
    if cable:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Cable* `{cable.get('label')}` "
            f"({provider}, corridor {corridor}, SLA {sla}h)"}})
    if impact.get("downstream_devices"):
        downstream = ", ".join(d["device"] for d in impact["downstream_devices"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Downstream impact*\n{downstream}"}})
    if impact.get("affected_agencies"):
        agencies = ", ".join(impact["affected_agencies"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Agencies affected*\n{agencies}"}})
    if enrichment.get("degraded"):
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text":
             "Partial enrichment: " + "; ".join(enrichment["degraded"])}]})
    return blocks


def _resolved_blocks(enrichment, ledger, downtime_str):
    alert = enrichment.get("alert", {})
    device = enrichment.get("device", {})
    iface = enrichment.get("interface", {})
    impact = ledger.get("impact", {})
    severity = impact.get("severity_class", SEVERITY_LOW)

    return [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"✅ {alert.get('name', 'Alert')} on {device.get('name')} — RESOLVED"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Device*\n{device.get('name')}"},
            {"type": "mrkdwn", "text": f"*Interface*\n{iface.get('name')}"},
            {"type": "mrkdwn", "text": f"*Site*\n{device.get('site')}"},
            {"type": "mrkdwn", "text": f"*Severity (was)*\n{severity}"},
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Downtime: *{downtime_str}*"},
        ]},
    ]


def _thread_summary(downtime_str, ledger):
    impact = ledger.get("impact", {})
    agencies = impact.get("affected_agencies") or []
    downstream = [d["device"] for d in impact.get("downstream_devices", [])]
    parts = [f"Resolved after *{downtime_str}*."]
    if downstream:
        parts.append(f"Downstream restored: {', '.join(downstream)}.")
    if agencies:
        parts.append(f"Agencies cleared: {', '.join(agencies)}.")
    return " ".join(parts)


def main():
    enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
    impact = json.loads(os.environ["IMPACT_JSON"])
    alert = enrichment.get("alert", {})
    fingerprint = alert.get("fingerprint")
    status = alert.get("status", "firing")

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    valkey_url = os.environ["VALKEY_URL"]

    unconfigured = _slack_unconfigured(token, channel)

    if not unconfigured:
        from slack_sdk import WebClient
        slack = WebClient(token=token)

    import valkey
    ledger_db = valkey.from_url(valkey_url, decode_responses=True)
    ledger_key = f"incident:{fingerprint}"

    if status == "firing":
        blocks = _firing_blocks(enrichment, impact)
        record = {
            "channel": channel,
            "first_seen": alert.get("started"),
            "impact": impact,
        }

        if unconfigured:
            print("=== firing (slack unconfigured) ===", file=sys.stderr)
            print(json.dumps({"channel": channel, "blocks": blocks}, indent=2),
                  file=sys.stderr)
            record["ts"] = "unconfigured.000000"
        else:
            resp = slack.chat_postMessage(
                channel=channel,
                text=f"{alert.get('name')} on {enrichment.get('device', {}).get('name')}",
                blocks=blocks,
            )
            record["ts"] = resp["ts"]

        try:
            ledger_db.set(ledger_key, json.dumps(record), ex=86400)
        except Exception as e:
            # A Valkey hiccup must not crash the step after we've already
            # posted; resolve will fall back to a fresh top-level post.
            print(f"warning: failed to persist incident ledger: {e}", file=sys.stderr)
        json.dump({"posted": not unconfigured, "status": "firing",
                   "ts": record["ts"], "fingerprint": fingerprint}, sys.stdout)
        return

    # resolved
    try:
        raw = ledger_db.get(ledger_key)
    except Exception as e:
        print(f"warning: ledger read failed, posting fresh resolved notice: {e}", file=sys.stderr)
        raw = None
    if not raw:
        # No ledger record (e.g. demo restart between firing and resolve).
        # Fall back to a fresh top-level resolved post.
        ledger_record = {"impact": impact, "first_seen": alert.get("started"),
                         "channel": channel, "ts": None}
    else:
        ledger_record = json.loads(raw)

    started = _parse_iso(ledger_record.get("first_seen"))
    ended = _parse_iso(alert.get("ended"))
    if not ended:
        ended = datetime.now(timezone.utc)
    downtime_secs = (ended - started).total_seconds() if started else 0
    downtime_str = _humanize_seconds(downtime_secs)
    blocks = _resolved_blocks(enrichment, ledger_record, downtime_str)
    summary = _thread_summary(downtime_str, ledger_record)

    if unconfigured:
        print("=== resolved update (slack unconfigured) ===", file=sys.stderr)
        print(json.dumps({"channel": ledger_record["channel"],
                          "ts": ledger_record["ts"],
                          "blocks": blocks,
                          "thread_summary": summary}, indent=2),
              file=sys.stderr)
    else:
        if ledger_record.get("ts"):
            slack.chat_update(
                channel=ledger_record["channel"],
                ts=ledger_record["ts"],
                text=f"{alert.get('name')} on {enrichment.get('device', {}).get('name')} — RESOLVED",
                blocks=blocks,
            )
            slack.chat_postMessage(
                channel=ledger_record["channel"],
                thread_ts=ledger_record["ts"],
                text=summary,
            )
        else:
            slack.chat_postMessage(
                channel=channel,
                text=f"{alert.get('name')} on {enrichment.get('device', {}).get('name')} — RESOLVED",
                blocks=blocks,
            )

    try:
        ledger_db.delete(ledger_key)
    except Exception as e:
        print(f"warning: ledger delete failed (24h TTL will reap it): {e}", file=sys.stderr)
    json.dump({"posted": not unconfigured, "status": "resolved",
               "downtime_seconds": int(downtime_secs),
               "fingerprint": fingerprint}, sys.stdout)


if __name__ == "__main__":
    main()
