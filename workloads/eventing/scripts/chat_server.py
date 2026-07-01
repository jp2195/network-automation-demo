#!/usr/bin/env python3
"""Interactive network Q&A agent — the console chat lane.

A long-lived FastAPI service (unlike the per-alert analyst Workflow)
behind the console's /api/chat ingress path. Same stack and degrade
contract as analyst.py: a Pydantic-AI agent over the read-only tools,
model config from the optional `ai-analyst` Secret — absent, /api/chat
answers 503 and the console panel renders its disabled state.

Read-only Q&A only: no gNMI/SNMP device pokes, no actions — the scenario
buttons stay the only way to touch the fabric. Blast-radius questions go
through corridor_impact (deterministic NetBox graph walk), so the model
narrates a computed answer instead of guessing one.

SSE contract per POST /api/chat ({"messages": [{role, content}...]}):
  event: tool   {"name", "summary"}   a tool call, as it happens
  event: token  {"text"}              text delta
  event: done   {"text"}              the complete final answer
  event: error  {"detail"}            user-safe failure, stream ends
Conversation state lives in the browser (full history in each request);
the server is stateless.
"""

import json
import os
import sys

from pydantic_ai import Agent
from pydantic_ai.messages import (FunctionToolCallEvent, ModelRequest,
                                  ModelResponse, PartDeltaEvent,
                                  PartStartEvent, TextPart, TextPartDelta,
                                  UserPromptPart)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import UsageLimits

import analyst
import analyst_tools
import corridor_impact

# Longest question a chip or a booth attendee reasonably produces; anything
# bigger is someone pasting a document into a demo box.
MAX_MESSAGE_CHARS = 4000
# Replayed turns per request (browser sends full history; the tail is
# plenty of context and bounds tokens per question).
MAX_HISTORY_MESSAGES = 20
# Model round-trips per question (tool calls + final answer). Smaller than
# the analyst's 24: chat answers should be a few lookups, not a deep dive.
DEFAULT_MAX_REQUESTS = 12
# Questions per pod lifetime — so an unattended booth box can't run up a
# hosted-model bill. Restart the pod to reset.
DEFAULT_LIFETIME_REQUESTS = 500

CHAT_TOOLS = [analyst_tools.query_prometheus,
              analyst_tools.query_prometheus_range,
              analyst_tools.query_loki,
              analyst_tools.query_netbox,
              corridor_impact.corridor_impact]

_INSTRUCTIONS = """\
You are the network operations assistant for Atlas DOT Region 7 (Atlanta
metro): an IS-IS network of 8 Nokia SR Linux nodes (tmc-* cores, hub-*
corridor hubs in an I-285 ring) and 4 legacy FRR field cabinets (fc-*,
single-homed to their hubs). You answer questions from operators and demo
audiences. Be concise, concrete, and honest about uncertainty.

Where answers live:
- NetBox is the source of truth: devices, cables (corridor/provider/SLA
  custom fields), agency tenancy as device tags, and each cabinet's ITS
  roster (CCTV, signal controllers, DMS, ramp meters) as devices at the
  cabinet's site. Use query_netbox for inventory questions.
- Any what-if or blast-radius question ("if corridor X goes down, what
  breaks?") MUST go through corridor_impact — it computes reachability
  from the topology model. Never eyeball the graph yourself.
- Live and recent state is Prometheus: ALERTS{alertstate="firing"} for
  what's firing now; count_over_time(ALERTS{alertname="..."}[6h]) style
  queries for "how many alerts" — and say the window out loud, retention
  is a few hours (this is a demo fabric, not a warehouse).
- Loki has device syslog and workflow logs. "Who changed X" is answered
  by SR Linux syslog: {source_type="syslog", host="<node>"} |~ "committed
  successfully by user" — that line names the operator.
- link:oper_state_with_meta and link_membership_info (labels link_id,
  corridor) are the link-level Prometheus series; raw srl_nokia_* series
  have node/interface labels only.

Rules: you are read-only — you cannot change the network, ack alerts, or
trigger scenarios; if asked to act, point at the console's scenario
buttons. Use AT MOST 6 tool calls per question, then answer with what you
have. If a query returns nothing, change the query — never repeat it
verbatim. Answer in plain prose (light markdown is fine); state numbers
and device names exactly as the tools returned them."""


def _max_requests():
    raw = os.environ.get("AI_MAX_REQUESTS", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_REQUESTS
    except ValueError:
        return DEFAULT_MAX_REQUESTS


def split_history(messages):
    """Validate a browser message list; return (prompt, model_messages).

    The last entry must be the user's new question; everything before it
    replays as Pydantic-AI message history (capped to the tail)."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    for m in messages:
        if not (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)):
            raise ValueError("each message needs role user|assistant and string content")
        if len(m["content"]) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} chars")
    if messages[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    history = []
    for m in messages[:-1][-MAX_HISTORY_MESSAGES:]:
        if m["role"] == "user":
            history.append(ModelRequest(parts=[UserPromptPart(m["content"])]))
        else:
            history.append(ModelResponse(parts=[TextPart(m["content"])]))
    return messages[-1]["content"], history


def sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def event_to_sse(ev):
    """Map one Pydantic-AI stream event to an (event, payload) pair, or
    None for events the browser has no use for (thinking, tool results —
    results flow back to the model, not the user)."""
    if isinstance(ev, FunctionToolCallEvent):
        args = ev.part.args
        summary = args if isinstance(args, str) else json.dumps(args or {})
        return "tool", {"name": ev.part.tool_name, "summary": summary[:200]}
    if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
        return ("token", {"text": ev.part.content}) if ev.part.content else None
    if isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
        delta = ev.delta.content_delta
        return ("token", {"text": delta}) if delta else None
    if isinstance(ev, AgentRunResultEvent):
        return "done", {"text": str(ev.result.output)}
    return None


def build_chat_agent(model):
    return Agent(
        model,
        instructions=_INSTRUCTIONS,
        tools=CHAT_TOOLS,
        retries=4,
        model_settings=analyst._model_settings(),
    )


def _env_model():
    """The ai-analyst Secret's model, or None (chat disabled) — same
    degrade contract as analyst.main()."""
    base_url = os.environ.get("AI_BASE_URL", "").strip()
    model_name = os.environ.get("AI_MODEL", "").strip()
    if not (base_url and model_name):
        return None
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    api_key = os.environ.get("AI_API_KEY", "").strip() or "not-needed"
    return OpenAIChatModel(
        model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def create_app(model=None, lifetime_requests=None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI()
    state = {"agent": None,
             "model": model,
             "remaining": lifetime_requests or int(
                 os.environ.get("CHAT_LIFETIME_REQUESTS", "").strip()
                 or DEFAULT_LIFETIME_REQUESTS)}

    def _agent():
        if state["agent"] is None:
            m = state["model"] or _env_model()
            if m is None:
                return None
            state["agent"] = build_chat_agent(m)
        return state["agent"]

    @app.get("/api/chat/status")
    def status():
        enabled = state["model"] is not None or _env_model() is not None
        return {"enabled": enabled,
                "model": os.environ.get("AI_MODEL") or None,
                "remaining_requests": state["remaining"]}

    @app.post("/api/chat")
    async def chat(request: Request):
        try:
            body = await request.json()
            prompt, history = split_history((body or {}).get("messages"))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        agent = _agent()
        if agent is None:
            return JSONResponse(
                {"detail": "AI chat disabled — ai-analyst Secret absent or "
                           "incomplete; see SECRETS.md"}, status_code=503)
        if state["remaining"] <= 0:
            return JSONResponse(
                {"detail": "chat request budget for this pod is exhausted"},
                status_code=429)
        state["remaining"] -= 1
        # The anti-repeat guard is process-global and tuned for one
        # investigation; a fresh question starts with a clean slate.
        analyst_tools._seen_calls.clear()

        async def stream():
            try:
                async with agent.run_stream_events(
                        prompt, message_history=history,
                        usage_limits=UsageLimits(
                            request_limit=_max_requests())) as events:
                    async for ev in events:
                        mapped = event_to_sse(ev)
                        if mapped:
                            yield sse(*mapped)
            except Exception as e:
                print(f"chat run failed: {e}", file=sys.stderr)
                yield sse("error", {"detail": "the assistant hit an error — "
                                              "try rephrasing the question"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0",
                port=int(os.environ.get("CHAT_PORT", "8080")))


if __name__ == "__main__":
    main()
