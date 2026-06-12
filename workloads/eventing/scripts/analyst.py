#!/usr/bin/env python3
"""AI incident analyst — the advisory lane (wave-2 feature 4).

sensor-ai-analyst triggers this on the same webhook alerts as
enriched-notify, in its own Workflow, so it can never block or modify
the deterministic pipeline. A Pydantic-AI agent investigates the alert
through read-only tools (analyst_tools) and returns an IncidentAnalysis.

Degrade contract: the `ai-analyst` Secret {base_url, api_key, model} is
optional — absent or incomplete, main() prints "AI disabled" and exits 0
(slack-bot pattern, SECRETS.md).

Output contract (consumed by postmortem.py and the alert-console panel):
exactly one stdout line `INCIDENT_ANALYSIS_V1 {json}` where the JSON is
the IncidentAnalysis plus the alert fingerprint, compact, line-final —
postmortem.extract_ai_analysis json.loads everything after the marker.
The analysis is advisory forever; remediation stays deterministic.
"""

import json
import os
import re
import sys

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.usage import UsageLimits

import analyst_tools
from constants import AI_ANALYSIS_MARKER

MAX_MODEL_REQUESTS = 24  # iteration cap; per-tool timeouts live in analyst_tools


class Evidence(BaseModel):
    source: str = Field(description="prometheus | netbox | loki | gnmi | snmp")
    query: str = Field(description="the exact query/path issued")
    observation: str = Field(description="what the result showed, one sentence")


class IncidentAnalysis(BaseModel):
    summary: str = Field(description="2-3 sentence incident summary for an on-call engineer")
    probable_root_cause: str
    recommendation: str = Field(description="operator next step — advisory only")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)


_INSTRUCTIONS = """\
You are the AI incident analyst for Atlas DOT Region 7 (Atlanta metro),
an IS-IS network of 8 Nokia SR Linux nodes (tmc-* cores, hub-* corridor
hubs in a ring) and 4 legacy FRR field cabinets (fc-*, single-homed to
hubs, SNMP-monitored). NetBox is the source of truth (devices, cables
with provider/corridor/SLA custom fields, agency tags on devices);
Prometheus carries gNMI+SNMP telemetry; Loki carries device and
workflow logs. IS-IS instance name: {isis}.

You receive one Alertmanager alert. Investigate it with the read-only
tools — verify current state, find the affected link's both ends, check
whether IS-IS rerouted, look for related log lines — then return an
IncidentAnalysis. Label shapes: raw srl_nokia_* series are labeled by
node/interface and have NO link_id label; link-level state lives in
link:oper_state_with_meta and link_membership_info (labeled by
link_id). If a query returns [], change the query — never repeat it
verbatim. Be concise and concrete; every claim in the summary
or root cause should be backed by an evidence entry (source, exact
query, observation).

Every turn must be exactly one tool call — never reply in plain text;
narration belongs in the final analysis, not between calls. Investigate
with AT MOST 6 tool calls, then you MUST finish by calling
submit_incident_analysis with your structured analysis — investigating
forever without submitting is a failure. If the evidence is incomplete,
submit anyway with a lower confidence and note what is missing.

A deterministic remediation lane may already have costed out a degraded
link (IS-IS metric 16777214 on both ends — visible via gnmi_get). You
are advisory only: never claim to have changed anything, and phrase the
recommendation as operator actions."""


def _model_settings():
    # Explicit output cap: thinking models (e.g. qwen3.5) otherwise
    # exhaust the provider-default budget on reasoning tokens before
    # any structured response lands (UnexpectedModelBehavior). A
    # ceiling, not an allocation — generous on purpose; runaway cost
    # is already bounded by request_limit + activeDeadlineSeconds.
    settings = {"max_tokens": 32768}
    # Optional Secret key temperature: low values (0.2) keep small
    # local models from wandering between tool calls. NOT defaulted —
    # OpenAI reasoning-class models reject non-default temperature, so
    # it is only sent when the user set it (same pattern as
    # reasoning_effort below).
    temp = os.environ.get("AI_TEMPERATURE", "").strip()
    if temp:
        try:
            settings["temperature"] = float(temp)
        except ValueError:
            print(f"ignoring non-numeric AI_TEMPERATURE {temp!r}",
                  file=sys.stderr)
    # Optional Secret key reasoning_effort: local thinking models loop
    # in reasoning on small contexts — "none" disables thinking on
    # Ollama's OpenAI-compat endpoint (smoke-verified). Omitted, the
    # endpoint's default applies; never sent unless the user set it.
    effort = os.environ.get("AI_REASONING_EFFORT", "").strip()
    if effort:
        settings["openai_reasoning_effort"] = effort
    return settings


def build_agent(model, isis_instance=None):
    return Agent(
        model,
        # A salient, imperative output-tool name: small local models
        # ignore the default "final_result" and investigate forever
        # (smoke-found); the explicit submit_* verb converges them.
        output_type=ToolOutput(
            IncidentAnalysis,
            name="submit_incident_analysis",
            description="Submit your final IncidentAnalysis. Call this "
                        "exactly once to finish the investigation.",
        ),
        # .replace, not .format: instruction edits may add PromQL/JSON
        # examples with literal braces, which .format would blow up on.
        instructions=_INSTRUCTIONS.replace(
            "{isis}", isis_instance or os.environ.get("ISIS_INSTANCE", "atlas")),
        tools=analyst_tools.ALL_TOOLS,
        # Small local models occasionally narrate in plain text between
        # tool calls; each such turn costs an output retry, so leave
        # headroom (wall-clock is still bounded by activeDeadlineSeconds).
        retries=8,
        model_settings=_model_settings(),
    )


def render_marker_line(analysis, fingerprint):
    payload = analysis.model_dump()
    # Stamped from the alert, not model-supplied — the postmortem joins
    # on it, so it must be exact.
    payload["fingerprint"] = fingerprint
    return f"{AI_ANALYSIS_MARKER} {json.dumps(payload, separators=(',', ':'))}"


def _maybe_slack(analysis, alert):
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    text = (f":robot_face: *AI analyst* — "
            f"{alert.get('labels', {}).get('alertname', 'alert')} "
            f"(confidence {analysis.confidence:.0%})\n"
            f"{analysis.summary}\n"
            f"*Probable root cause:* {analysis.probable_root_cause}\n"
            f"*Recommendation:* {analysis.recommendation}")
    if not (token and channel):
        print(f"slack-bot secret absent — analysis (stderr copy):\n{text}",
              file=sys.stderr)
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=token).chat_postMessage(channel=channel, text=text)
    except Exception as e:
        print(f"slack post failed (non-fatal): {e}", file=sys.stderr)


def main():
    base_url = os.environ.get("AI_BASE_URL", "").strip()
    model_name = os.environ.get("AI_MODEL", "").strip()
    api_key = os.environ.get("AI_API_KEY", "").strip() or "not-needed"
    if not (base_url and model_name):
        print("AI disabled — ai-analyst Secret absent or incomplete; "
              "see SECRETS.md (deterministic pipeline unaffected)")
        return

    body = json.loads(os.environ["ALERT_JSON"])
    alert = (body.get("alerts") or [{}])[0]
    if alert.get("status", "firing") != "firing":
        print("resolved event — analysis runs on firing alerts only")
        return
    fingerprint = re.sub(r"[^A-Za-z0-9]", "", alert.get("fingerprint") or "")
    if not fingerprint:
        print("alert carries no fingerprint — skipping analysis")
        return

    # Imported here, not at module top: the OpenAI client dep only
    # matters when the lane is enabled (tests run on pydantic-ai-slim).
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    model = OpenAIChatModel(
        model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))

    agent = build_agent(model)
    prompt = ("Analyze this alert and produce an IncidentAnalysis.\n"
              "Alert JSON:\n" + json.dumps(alert, indent=2))
    from pydantic_ai import capture_run_messages
    with capture_run_messages() as messages:
        try:
            result = agent.run_sync(
                prompt,
                usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS))
        except Exception:
            # Compact transcript to stderr (pod logs) so a failed run is
            # triageable from the cluster — which tools the model called
            # and what came back (runbook: "AI analyst produced nothing").
            print("--- agent transcript (truncated) ---", file=sys.stderr)
            for m in messages:
                for p in getattr(m, "parts", []):
                    content = str(getattr(p, "content", getattr(p, "args", "")))
                    print(f"{type(p).__name__:22s} "
                          f"{getattr(p, 'tool_name', '') or '':28s} "
                          f"{content[:140]}", file=sys.stderr)
            raise

    print(render_marker_line(result.output, fingerprint))
    _maybe_slack(result.output, alert)


if __name__ == "__main__":
    main()
