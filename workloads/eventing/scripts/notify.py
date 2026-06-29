#!/usr/bin/env python3
"""Post a Slack alert message and reconcile firing↔resolved transitions.

Inputs (env):
  ENRICHMENT_JSON, IMPACT_JSON  — outputs from prior WFT steps
  SLACK_BOT_TOKEN, SLACK_CHANNEL_ID  — bot creds (xoxb-...)
  VALKEY_URL  — incident ledger, e.g. valkey://valkey.valkey.svc.cluster.local:6379/2
  GRAFANA_URL — base URL for the per-incident dashboard link (optional)

Behavior:
  - On firing: chat.postMessage a severity-colored Block Kit attachment,
    persist {ts, channel, first_seen, impact} in Valkey under
    incident:<fingerprint> with 24h TTL.
  - On resolved: load ledger, chat.update the original message in place
    (green ✅ header + downtime), then chat.postMessage a thread reply with
    the resolution summary.
  - Always: writes the alert status to /tmp/argo/status (Argo output
    parameter gating the postmortem step on the resolved path).

If SLACK_BOT_TOKEN or SLACK_CHANNEL_ID is empty (the Slack `slack-bot`
Secret hasn't been created), the script short-circuits to stderr-printed
payloads — the demo runs without real Slack credentials. See SECRETS.md
for how to opt into real Slack posting.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from constants import SEVERITY_HIGH, SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_WARNING
from timefmt import humanize_seconds, parse_iso


def _slack_unconfigured(token, channel):
    return not token or not channel


def _valkey_retry(op, attempts=3):
    # A transient Valkey blip shouldn't orphan the firing↔resolved
    # correlation; the callers' except blocks handle a final failure.
    for i in range(1, attempts + 1):
        try:
            return op()
        except Exception as e:
            if i == attempts:
                raise
            print(f"warning: valkey attempt {i}/{attempts} failed, retrying: {e}",
                  file=sys.stderr)
            time.sleep(0.5 * i)


# --- Presentation -----------------------------------------------------------
# The message reads top-down as an operator triages: a severity color bar, a
# one-line verdict that fuses severity with the modeled backup state ("is
# traffic still protected?"), then identity, impact, and provenance demoted to
# muted context. The raw alertname/link_id/fingerprint stay in the footer for
# anyone who keys on them, but the headline speaks in plain operator terms.

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana.127-0-0-1.nip.io:8080")

# severity_class -> (emoji, attachment color bar, label)
_SEV_STYLE = {
    SEVERITY_HIGH:    ("🔴", "#D7263D", "High"),
    SEVERITY_MEDIUM:  ("🟠", "#E8912D", "Medium"),
    SEVERITY_WARNING: ("🟡", "#ECB22E", "Warning"),
    SEVERITY_LOW:     ("🔵", "#2D7FF9", "Low"),
}
_RESOLVED_COLOR = "#2EB67D"

# Raw Prometheus alertnames -> human, operator-facing titles.
_ALERT_TITLES = {
    "SRLInterfaceOperDown":     "Interface down",
    "SRLInterfaceFlapping":     "Interface flapping",
    "SRLOpticalDegrading":      "Optical degrading",
    "SRLInterfaceErrorsHigh":   "Errors climbing",
    "CabinetInterfaceOperDown": "Cabinet uplink down",
    "ConfigDrift":              "Config drift",
}
_ACRONYMS = {"adot": "ADOT", "dot": "DOT", "ta": "TA", "tmc": "TMC", "noc": "NOC"}


def _alert_title(name):
    return _ALERT_TITLES.get(name, name or "Alert")


def _pretty_agency(slug):
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in slug.split("-"))


def _short_time(iso):
    t = parse_iso(iso)
    return t.strftime("%H:%M UTC") if t else ""


def _fp_uid(fingerprint):
    return re.sub(r"[^A-Za-z0-9]", "", fingerprint or "").lower()


def _backup_line(backup):
    """One-line, operator-facing verdict on the modeled backup path."""
    if not backup or backup.get("state") in (None, "unknown"):
        return None
    if backup.get("available"):
        if backup.get("state") == "up":
            return "✅ traffic protected by corridor ring"
        return f"⚠️ corridor ring degraded — {backup.get('detail', '')}"
    return "⛔ no protected path — single-homed cabinet"


def _identity_line(enrichment):
    a = enrichment.get("alert", {})
    dev = enrichment.get("device", {})
    ifc = enrichment.get("interface", {})
    cable = enrichment.get("cable") or {}
    cf = cable.get("custom_fields") or {}
    corridor = a.get("corridor") or (cable.get("site_group") or {}).get("slug") \
        or cf.get("corridor") or "unknown"
    line = f"`{ifc.get('name')}`   ·   {dev.get('site')}   ·   {corridor}"
    if cable.get("label"):
        provider = (cable.get("owner") or {}).get("name") or cf.get("provider") or "unknown"
        sla = cf.get("restoration_sla_hours", "?")
        line += f"   ·   `{cable.get('label')}` ({provider}, SLA {sla}h)"
    return line


def _footer_line(enrichment, when_label, when_iso):
    a = enrichment.get("alert", {})
    uid = _fp_uid(a.get("fingerprint"))
    parts = [a.get("name"), a.get("link_id"),
             f"{when_label} {_short_time(when_iso)}".strip(),
             f"`{uid[:8]}`" if uid else None,
             f"<{GRAFANA_URL}/d/incident-{uid}|Open in Grafana ↗>" if uid else None]
    return "   ·   ".join(p for p in parts if p)


def _firing_blocks(enrichment, impact):
    a = enrichment.get("alert", {})
    dev = enrichment.get("device", {})
    severity = impact.get("severity_class", SEVERITY_LOW)
    emoji, color, sev_label = _SEV_STYLE.get(severity, _SEV_STYLE[SEVERITY_LOW])

    verdict = f"*{sev_label} severity*"
    backup = _backup_line(impact.get("backup_path"))
    if backup:
        verdict += f"   ·   {backup}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "emoji": True,
            "text": f"{emoji} {_alert_title(a.get('name'))} · {dev.get('name')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": verdict}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": _identity_line(enrichment)}]},
    ]
    fields = []
    down = impact.get("downstream_devices") or []
    if down:
        fields.append({"type": "mrkdwn",
            "text": f"*Downstream ({len(down)})*\n" + ", ".join(d["device"] for d in down)})
    if impact.get("affected_agencies"):
        fields.append({"type": "mrkdwn", "text": "*Agencies*\n"
            + ", ".join(_pretty_agency(x) for x in impact["affected_agencies"])})
    if fields:
        blocks.append({"type": "section", "fields": fields})
    if enrichment.get("degraded"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": "⚠ partial enrichment: " + "; ".join(enrichment["degraded"])}]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": _footer_line(enrichment, "detected", a.get("started"))}]})
    return color, blocks


def _resolved_blocks(enrichment, ledger, downtime_str):
    a = enrichment.get("alert", {})
    dev = enrichment.get("device", {})
    impact = ledger.get("impact", {})
    backup = impact.get("backup_path") or {}
    held = "   ·   ✅ corridor ring held throughout" if backup.get("state") == "up" else ""

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "emoji": True,
            "text": f"✅ {_alert_title(a.get('name'))} · {dev.get('name')} — resolved"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"Recovered after *{downtime_str}*" + held}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": _identity_line(enrichment)}]},
    ]
    down = impact.get("downstream_devices") or []
    if down:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*Restored*\n" + ", ".join(d["device"] for d in down)}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": _footer_line(enrichment, "resolved", a.get("ended"))}]})
    return _RESOLVED_COLOR, blocks


def _thread_summary(downtime_str, ledger):
    impact = ledger.get("impact", {})
    agencies = impact.get("affected_agencies") or []
    downstream = [d["device"] for d in impact.get("downstream_devices", [])]
    parts = [f"Resolved after *{downtime_str}*."]
    if downstream:
        parts.append(f"Downstream restored: {', '.join(downstream)}.")
    if agencies:
        parts.append(f"Agencies cleared: {', '.join(_pretty_agency(a) for a in agencies)}.")
    return " ".join(parts)


def main():
    enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
    impact = json.loads(os.environ["IMPACT_JSON"])
    alert = enrichment.get("alert", {})
    fingerprint = alert.get("fingerprint")
    status = alert.get("status", "firing")
    dev_name = enrichment.get("device", {}).get("name")
    headline = f"{_alert_title(alert.get('name'))} · {dev_name}"

    # The WFT maps /tmp/argo/status to an output parameter that gates the
    # postmortem step (when: == resolved). A write failure degrades to the
    # parameter's `default: firing` — postmortem skipped, notify unharmed.
    try:
        os.makedirs("/tmp/argo", exist_ok=True)
        with open("/tmp/argo/status", "w") as f:
            f.write(status)
    except OSError as e:
        print(f"warning: could not write /tmp/argo/status: {e}", file=sys.stderr)

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
        color, blocks = _firing_blocks(enrichment, impact)
        attachments = [{"color": color, "blocks": blocks}]
        record = {
            "channel": channel,
            "first_seen": alert.get("started"),
            "impact": impact,
        }

        if unconfigured:
            print("=== firing (slack unconfigured) ===", file=sys.stderr)
            print(json.dumps({"channel": channel, "attachments": attachments}, indent=2),
                  file=sys.stderr)
            record["ts"] = "unconfigured.000000"
        else:
            resp = slack.chat_postMessage(
                channel=channel, text=headline, attachments=attachments)
            record["ts"] = resp["ts"]

        try:
            _valkey_retry(lambda: ledger_db.set(ledger_key, json.dumps(record), ex=86400))
        except Exception as e:
            # A Valkey hiccup must not crash the step after we've already
            # posted; resolve will fall back to a fresh top-level post.
            print(f"warning: failed to persist incident ledger: {e}", file=sys.stderr)
        json.dump({"posted": not unconfigured, "status": "firing",
                   "ts": record["ts"], "fingerprint": fingerprint}, sys.stdout)
        return

    # resolved
    try:
        raw = _valkey_retry(lambda: ledger_db.get(ledger_key))
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

    started = parse_iso(ledger_record.get("first_seen"))
    ended = parse_iso(alert.get("ended"))
    if not ended:
        ended = datetime.now(timezone.utc)
    downtime_secs = (ended - started).total_seconds() if started else 0
    downtime_str = humanize_seconds(downtime_secs)
    color, blocks = _resolved_blocks(enrichment, ledger_record, downtime_str)
    attachments = [{"color": color, "blocks": blocks}]
    summary = _thread_summary(downtime_str, ledger_record)

    if unconfigured:
        print("=== resolved update (slack unconfigured) ===", file=sys.stderr)
        print(json.dumps({"channel": ledger_record["channel"],
                          "ts": ledger_record["ts"],
                          "attachments": attachments,
                          "thread_summary": summary}, indent=2),
              file=sys.stderr)
    else:
        if ledger_record.get("ts"):
            slack.chat_update(
                channel=ledger_record["channel"],
                ts=ledger_record["ts"],
                text=f"{headline} — resolved",
                attachments=attachments,
            )
            slack.chat_postMessage(
                channel=ledger_record["channel"],
                thread_ts=ledger_record["ts"],
                text=summary,
            )
        else:
            slack.chat_postMessage(
                channel=channel,
                text=f"{headline} — resolved",
                attachments=attachments,
            )

    try:
        _valkey_retry(lambda: ledger_db.delete(ledger_key))
    except Exception as e:
        print(f"warning: ledger delete failed (24h TTL will reap it): {e}", file=sys.stderr)
    json.dump({"posted": not unconfigured, "status": "resolved",
               "downtime_seconds": int(downtime_secs),
               "first_seen": ledger_record.get("first_seen"),
               # False = this resolve ran on the no-ledger fallback (e.g.
               # a duplicate resolved episode) — postmortem treats the
               # report as degraded and won't clobber a stored one.
               "ledger_found": bool(raw),
               "fingerprint": fingerprint}, sys.stdout)


if __name__ == "__main__":
    main()
