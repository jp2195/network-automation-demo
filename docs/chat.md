# Ask the network — the console chat lane

An interactive, **read-only** Q&A agent embedded in the scenario console
(`console.127-0-0-1.nip.io`). Where the [AI incident analyst](ai-analyst.md)
investigates one alert when a sensor fires, the chat answers whatever a demo
audience types, live. Both lanes share the same stack (Pydantic-AI over the
read-only tool set) and the same optional `ai-analyst` Secret — no Secret, no
chat, and the rest of the demo is untouched.

## What it can answer

| Question shape | Where the answer comes from |
| --- | --- |
| "If corridor I-285 goes down, what breaks?" | `corridor_impact` — a deterministic reachability walk over the NetBox cable graph |
| "How many CCTV cameras are on GA-400?" | NetBox (each cabinet's ITS roster is seeded as devices at its site) |
| "Which agencies ride on fc-i20e?" | NetBox device tags |
| "What alerts are firing right now?" | `firing_alerts` — the exact `ALERTS` query the console status tile runs (same Watchdog/InfoInhibitor filter), so chat and tile always agree |
| "How many alerts in the last hour?" | Prometheus range queries over `ALERTS` (the agent says the window — retention is hours, not weeks) |
| "Who last changed hub-e?" | Loki, SR Linux syslog (`committed successfully by user …`) |

## Architecture

```
browser (console chat panel)
  └─ POST console…/api/chat            SSE stream back
       └─ Traefik ingress path-routes /api/chat (same origin, no proxy code)
            └─ chat-agent Deployment   workloads/chat-agent/, argo-events ns
                 └─ Pydantic-AI agent  scripts/chat_server.py
                      tools: firing_alerts · corridor_impact
                             query_prometheus · query_prometheus_range
                             query_loki · query_netbox
```

- **Stateless server.** The browser replays the conversation (capped) with
  every request; the pod holds no session state.
- **SSE contract.** `tool` (name + args summary, feeds the visible trace),
  `token` (text delta), `done` (final answer), `error`. The panel renders
  tool calls as `›` lines in the event-log vernacular.
- **Computed what-ifs.** `scripts/corridor_impact.py` cuts every cable whose
  `corridor` custom field matches (loose spelling: `I285` ≈ `I-285` and
  `I-285 East`), rebuilds adjacency from the surviving cable terminations,
  BFS-walks from the TMCs, and reports isolated routers, their agencies, and
  the ITS assets at the dark sites. The model narrates; the graph decides.
  The cable `corridor` custom field is rendered into the NetBox seed from
  `spec/atlanta.yaml` for exactly this purpose (it is *not* derivable from a
  cable's endpoint sites — the ring spans sites grouped under other
  corridors).
- **Deterministic tools for demo-critical questions.** The design rule this
  lane follows: any question a demo audience *will* ask gets a computed,
  purpose-named tool (`corridor_impact`, `firing_alerts`) rather than
  trusting a small local model to pick the right generic query. Live-found
  counterexample that motivated `firing_alerts`: asked "what alerts are
  firing?", the model searched Loki (logs — no alerts there) and declared
  all-clear while the status tile showed alerts firing. The generic tools
  stay for the long tail of unscripted questions.

## Guardrails

Chat v1 exposes **no device-level tools** (no gNMI/SNMP) and no actions —
if asked to change something it points at the scenario buttons. Bounds:
≤6 tool calls per question (prompt) with `AI_MAX_REQUESTS` (default 12) as
the hard per-question round-trip cap, 4 000-char inputs, 20 replayed history
messages, and `CHAT_LIFETIME_REQUESTS` (default 500) questions per pod so an
unattended booth box can't run up a hosted-model bill — restart the pod to
reset. Tool results are byte-bounded and NetBox access is GET-only
(`analyst_tools.py` allowlists, shared with the analyst).

## Pieces

| Piece | Path |
| --- | --- |
| Server (FastAPI + agent + SSE) | `workloads/eventing/scripts/chat_server.py` |
| Corridor what-if walk | `workloads/eventing/scripts/corridor_impact.py` |
| Deployment / Service | `workloads/chat-agent/` |
| Ingress path (`/api/chat`) | `workloads/console/ingress.yaml` |
| Image | `images/chat-agent/Dockerfile` (python:3.14-slim) |
| Console panel | `tools/console/static/` (`chatInit` in `app.js`) |
| Tests | `scripts/test_chat_server.py`, `scripts/test_corridor_impact.py` |

Run the tests:

```bash
cd workloads/eventing/scripts
python3 -m unittest test_corridor_impact -v
uv run --quiet --with "pydantic-ai-slim[openai]" --with fastapi --with httpx \
  python3 -m unittest test_chat_server -v
```
