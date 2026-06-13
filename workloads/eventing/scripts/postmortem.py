#!/usr/bin/env python3
"""Generate a Markdown postmortem when an incident resolves.

Final step of the enriched-notify Workflow. The WFT `when:` gate keeps
it Skipped on firing alerts; the status check here is belt-and-braces.

Inputs (env):
  ENRICHMENT_JSON — enrich step output (resolved-time NetBox context;
                    the cable graph is static, so it matches firing-time)
  IMPACT_JSON     — analyze step output
  NOTIFY_JSON     — notify step output {status, downtime_seconds,
                    first_seen, fingerprint, posted}. notify deletes the
                    incident:<fp> ledger key, so first_seen rides through
                    here instead of being re-read from Valkey.
  VALKEY_URL      — postmortem store (DB 2)
  PROM_URL        — link-state series around the incident window (optional)
  LOKI_URL        — device log excerpts + AI analyst lookup (optional)

Output: Markdown stored at postmortem:<fingerprint> (TTL 7d, see
`make postmortem`) plus an audit line on stdout for the Loki audit feed.
Prom/Loki lookups degrade to omitted sections — only a Valkey store
failure fails the step.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from constants import (
    AI_ANALYSIS_MARKER,
    POSTMORTEM_KEY_PREFIX,
    POSTMORTEM_TTL_SECONDS,
)
from loki import loki_query_range
from prom import prom_query_range
from timefmt import humanize_seconds, parse_iso

# link:oper_state_with_meta values (SRL oper-state enum).
_STATE = {"1": "UP", "2": "DOWN"}


def _section_timeline(first_seen, ended, duration_secs):
    return "\n".join([
        "## Timeline",
        "",
        "| event | time (UTC) |",
        "|---|---|",
        f"| first seen | {first_seen or 'unknown'} |",
        f"| resolved | {ended or 'unknown'} |",
        "",
        f"**Duration:** {humanize_seconds(duration_secs)}",
        "",
    ])


def _section_alert(alert, device, iface, cable):
    cf = cable.get("custom_fields", {}) if cable else {}
    lines = [
        "## Alert",
        "",
        f"- **Alert:** {alert.get('name')} (severity label: {alert.get('severity')})",
        f"- **Device:** {device.get('name')} "
        f"({device.get('role') or '?'}, {device.get('site') or '?'})",
        f"- **Interface:** {iface.get('name') or '?'}",
    ]
    if alert.get("link_id"):
        lines.append(f"- **Link:** {alert['link_id']}")
    if cable:
        # Same precedence as notify.py: owner is the NetBox 4.x model,
        # custom_fields/site_group are back-compat fallbacks.
        provider = ((cable.get("owner") or {}).get("name")
                    or cf.get("provider") or "unknown provider")
        corridor = (alert.get("corridor")
                    or (cable.get("site_group") or {}).get("slug")
                    or cf.get("corridor") or "unknown")
        lines.append(f"- **Cable:** `{cable.get('label')}` "
                     f"({provider}, corridor {corridor})")
    lines.append("")
    return "\n".join(lines)


def _section_impact(impact):
    lines = ["## Impact", ""]
    downstream = impact.get("downstream_devices") or []
    if downstream:
        lines += ["| downstream device | interface | cable |", "|---|---|---|"]
        lines += [f"| {d.get('device')} | {d.get('interface')} | {d.get('cable_label')} |"
                  for d in downstream]
        lines.append("")
    else:
        lines += ["_No downstream devices — ring redundancy held._", ""]
    agencies = impact.get("affected_agencies") or []
    lines += [
        f"- **Agencies affected:** {', '.join(agencies) if agencies else 'none'}",
        f"- **Severity class:** {impact.get('severity_class', '?')}",
        "",
    ]
    return "\n".join(lines)


def _section_sla(duration_secs, sla_hours):
    try:
        sla_secs = float(sla_hours) * 3600
    except (TypeError, ValueError):
        return ""
    if duration_secs <= sla_secs:
        verdict = "✅ within SLA"
    else:
        verdict = (f"❌ SLA BREACH (exceeded by "
                   f"{humanize_seconds(duration_secs - sla_secs)})")
    return "\n".join([
        "## SLA",
        "",
        f"- **Restoration SLA:** {sla_hours}h",
        f"- **Actual:** {humanize_seconds(duration_secs)}",
        f"- **Verdict:** {verdict}",
        "",
    ])


def _section_telemetry(series):
    if not series:
        return ""
    lines = ["## Link-state telemetry", ""]
    for s in series:
        m = s.get("metric", {})
        label = f"{m.get('node', '?')}:{m.get('interface', '?')}"
        transitions, prev = [], None
        for ts, val in s.get("values", []):
            if val == prev:
                continue
            t = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%SZ")
            transitions.append(f"{t} {_STATE.get(val, val)}")
            prev = val
        lines.append(f"- `{label}` — "
                     f"{' → '.join(transitions) if transitions else 'no samples'}")
    lines.append("")
    return "\n".join(lines)


def _section_logs(log_lines):
    if not log_lines:
        return ""
    lines = ["## Device log excerpts", "", "```"]
    lines += [line[:300] for _, line in log_lines]
    lines += ["```", ""]
    return "\n".join(lines)


def _section_ai(analysis):
    if not analysis:
        return ""
    lines = ["## Analyst narrative (AI)", "",
             (analysis.get("summary") or "").strip() or "_no summary_", ""]
    if analysis.get("probable_root_cause"):
        lines.append(f"- **Probable root cause:** {analysis['probable_root_cause']}")
    if analysis.get("recommendation"):
        lines.append(f"- **Recommendation:** {analysis['recommendation']}")
    if analysis.get("confidence") is not None:
        lines.append(f"- **Confidence:** {analysis['confidence']}")
    evidence = analysis.get("evidence") or []
    if evidence:
        lines += ["", "| source | query | observation |", "|---|---|---|"]
        lines += [f"| {e.get('source', '')} | `{e.get('query', '')}` "
                  f"| {e.get('observation', '')} |" for e in evidence]
    lines.append("")
    return "\n".join(lines)


def extract_ai_analysis(loki_lines):
    """Newest line carrying the marker + parseable JSON wins; None if none.

    loki_query_range returns lines sorted ascending, so iterate in reverse
    rather than re-sorting (which would tie-break equal timestamps on the
    line text — arbitrary, not "newest")."""
    for _, line in reversed(list(loki_lines)):
        idx = line.find(AI_ANALYSIS_MARKER)
        if idx < 0:
            continue
        payload = line[idx + len(AI_ANALYSIS_MARKER):].strip()
        try:
            return json.loads(payload)
        except ValueError:
            continue
    return None


def build_markdown(enrichment, impact, notify_result, *, series=(), log_lines=(),
                   analysis=None, generated_at=""):
    alert = enrichment.get("alert", {})
    device = enrichment.get("device", {})
    iface = enrichment.get("interface", {})
    cable = enrichment.get("cable", {})
    cf = cable.get("custom_fields", {}) if cable else {}

    first_seen = notify_result.get("first_seen") or alert.get("started")
    duration = notify_result.get("downtime_seconds", 0)

    title = f"# Postmortem — {alert.get('name', 'Alert')} on {device.get('name', '?')}"
    if alert.get("link_id"):
        title += f" ({alert['link_id']})"

    fingerprint = alert.get("fingerprint") or notify_result.get("fingerprint")
    parts = [
        title,
        "",
        f"- **Fingerprint:** `{fingerprint}`",
        f"- **Generated:** {generated_at}",
        "",
        _section_timeline(first_seen, alert.get("ended"), duration),
        _section_alert(alert, device, iface, cable),
        _section_impact(impact),
        _section_sla(duration, cf.get("restoration_sla_hours")),
        _section_telemetry(series),
        _section_logs(log_lines),
        _section_ai(analysis),
    ]
    return "\n".join(p for p in parts if p) + "\n"


def store(client, fingerprint, markdown, degraded=False):
    """Persist the report. A degraded report (no ledger → zero duration,
    collapsed log window) must never clobber a good one already stored
    for this fingerprint — Alertmanager can emit a second resolved
    episode for the same alert after the ledger was consumed
    (smoke-found 2026-06-13)."""
    key = POSTMORTEM_KEY_PREFIX + fingerprint
    if degraded and client.set(key, markdown, ex=POSTMORTEM_TTL_SECONDS,
                               nx=True) is None:
        print(f"degraded postmortem for {fingerprint} skipped — a stored "
              f"report already exists", file=sys.stderr)
        return False
    if not degraded:
        client.set(key, markdown, ex=POSTMORTEM_TTL_SECONDS)
    return True


def main():
    enrichment = json.loads(os.environ["ENRICHMENT_JSON"])
    impact = json.loads(os.environ["IMPACT_JSON"])
    notify_result = json.loads(os.environ["NOTIFY_JSON"])

    if notify_result.get("status") != "resolved":
        print("alert status is not resolved — no postmortem", file=sys.stderr)
        return

    alert = enrichment.get("alert", {})
    fingerprint = alert.get("fingerprint") or notify_result.get("fingerprint")
    if not fingerprint:
        sys.exit("no fingerprint on alert — cannot key the postmortem")
    # Alertmanager fingerprints are hex; strip anything else so the value
    # is safe to interpolate into LogQL filters and the stdout audit line
    # (a stray quote/newline would break the query or split the log line).
    fingerprint = re.sub(r"[^A-Za-z0-9]", "", fingerprint)
    if not fingerprint:
        sys.exit("fingerprint has no safe characters — cannot key the postmortem")

    first_seen = parse_iso(notify_result.get("first_seen") or alert.get("started"))
    ended = parse_iso(alert.get("ended")) or datetime.now(timezone.utc)
    duration = notify_result.get("downtime_seconds")
    if duration is None:
        duration = int((ended - first_seen).total_seconds()) if first_seen else 0
        notify_result["downtime_seconds"] = duration
        if not first_seen:
            print("warning: no first_seen/downtime available — duration "
                  "defaulted to 0; SLA verdict unreliable", file=sys.stderr)

    # Telemetry/log window: a little context either side of the incident.
    start = (first_seen.timestamp() if first_seen else ended.timestamp() - 600) - 300
    end = ended.timestamp() + 300

    prom_url = os.environ.get("PROM_URL", "")
    loki_url = os.environ.get("LOKI_URL", "")
    link_id = alert.get("link_id")

    series = []
    # link_id is an alert label; only interpolate spec-shaped values into
    # the PromQL selector (anything else: skip the telemetry section).
    if prom_url and link_id and re.fullmatch(r"[A-Za-z0-9_-]+", link_id):
        series = prom_query_range(
            prom_url, f'link:oper_state_with_meta{{link_id="{link_id}"}}',
            start, end)

    log_lines, analysis = [], None
    if loki_url:
        device_name = enrichment.get("device", {}).get("name", "")
        if device_name:
            raw = loki_query_range(
                loki_url, '{namespace="clabernetes"} |~ "(?i)commit|admin-state"',
                start, end)
            log_lines = [(ts, ln) for ts, ln in raw if device_name in ln][:20]
        analysis = extract_ai_analysis(loki_query_range(
            loki_url,
            f'{{namespace="argo-events"}} |= "{AI_ANALYSIS_MARKER}" |= "{fingerprint}"',
            start, end + 300))

    md = build_markdown(
        enrichment, impact, notify_result,
        series=series, log_lines=log_lines, analysis=analysis,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    import valkey
    client = valkey.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    try:
        store(client, fingerprint, md,
              degraded=not notify_result.get("ledger_found", True))
    except Exception as e:
        # Don't lose the artifact — it lands in the step log at least.
        print(md, file=sys.stderr)
        sys.exit(f"failed to store postmortem: {e}")

    device_name = enrichment.get("device", {}).get("name", "")
    print(f"postmortem generated fingerprint={fingerprint} device={device_name} "
          f"duration={humanize_seconds(duration)} bytes={len(md)}")


if __name__ == "__main__":
    main()
