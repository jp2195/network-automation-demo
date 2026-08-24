# AI incident analyst

An **optional, advisory, read-only** agent that investigates an alert and posts a
structured finding — *what probably happened and what to do* — as a thread reply
under the incident card. It is the "intelligent" assist on top of the
deterministic enrich → analyze → notify pipeline, and it is deliberately fenced
so it can never break or change that pipeline.

It runs in its **own** Argo Workflow (`ai-analyze-*`), triggered in parallel with
the deterministic `enrich-notify` Workflow — so a slow, failed, or absent model
never blocks alerting, enrichment, or notification.

## Safety boundaries (the whole point)

- **Read-only.** The agent has exactly six tools, all GET/query — see below. It
  cannot configure, cut, cost-out, or change anything.
- **Advisory forever.** Its `recommendation` is text for a human. It never
  executes remediation. The closed-loop remediation lane (IS-IS cost-out on a
  gray failure) is a **separate, deterministic** Workflow with no model in it.
- **Optional, fail-open.** The `ai-analyst` Secret (`base_url`, `api_key`,
  `model`) is optional. Absent or incomplete → the step prints `AI disabled`,
  exits 0, and the deterministic pipeline is unaffected. See
  [`SECRETS.md`](../SECRETS.md) to enable it.
- **Bounded.** Each run is capped by a tool-call `request_limit`
  (`AI_MAX_REQUESTS`), a wall-clock `activeDeadlineSeconds: 900`, a repeat-guard
  (re-calling a tool with identical args gets a corrective nudge), and
  byte-bounded tool results (so large telemetry can't blow the context window).

## What it produces

A validated `IncidentAnalysis` (the model is forced to emit it via an output
tool, which keeps small local models on-contract):

| field | meaning |
|---|---|
| `summary` | 2–3 sentence incident summary for an on-call engineer |
| `probable_root_cause` | the model's best explanation |
| `recommendation` | operator next step — **advisory only** |
| `confidence` | 0.0–1.0 |
| `evidence` | list of `{source, query, observation}` — the tool calls it relied on |

That finding goes two places:
1. **Slack** — a `🤖 AI analyst` reply threaded under the incident card (summary,
   root cause, recommendation, confidence).
2. An `INCIDENT_ANALYSIS_V1 {…json…}` **marker line** on stdout, consumed by
   `postmortem.py` and surfaced in the per-incident Grafana dashboard panel.

## Read-only tools

| tool | what it reaches |
|---|---|
| `query_prometheus(promql)` | instant PromQL — gNMI + SNMP telemetry |
| `query_prometheus_range(promql, minutes)` | range PromQL (clamped) — trends/flaps |
| `query_loki(logql, minutes, around)` | device + daemon logs. `around` (an ISO instant, typically the alert's `startsAt`) weights the window **mostly before** that moment — a cause precedes its effect. Timestamps come back pre-formatted as ISO-8601 UTC so the model quotes them rather than converting epochs. |
| `query_netbox(path, params)` | the source of truth — devices, cables, corridor/provider/SLA, agency tags (GET only) |
| `gnmi_get(node, path)` | live SR Linux state (native paths — `admin-state`, `oper-state`, `oper-down-reason`, IS-IS adjacency) |
| `snmp_get(node, oid)` | live state from the FRR cabinets (SNMP) |

The system prompt orients the model to the topology (8 SR Linux nodes — `tmc-*`
cores, `hub-*` corridor-hub ring; 4 single-homed `fc-*` FRR cabinets), with
NetBox as the source of truth.

## When it runs

The `ai-analyst` Sensor sees the same alert families as the deterministic
enrich-notify lane: `SRLInterfaceOperDown`, `SRLInterfaceFlapping`,
`SRLOpticalDegrading`, `SRLInterfaceErrorsHigh`, `CabinetInterfaceOperDown`
(the legacy SNMP edge), and `ConfigDrift`.

## Models

Any OpenAI-compatible endpoint — a hosted frontier model (best, zero tuning) or a
**local** model via Ollama (zero-cost, self-hosted, no data egress). On Apple
Silicon, an **MLX** build is fastest; `qwen3.6:35b-mlx` and the smaller
`qwen3.5:9b` are both validated. Exact recipes in [`SECRETS.md`](../SECRETS.md).

## Honest limits

- It reasons over **live state at run time**, and an incident often gets fixed
  while the agent is still working. A restore that lands mid-run shows up in
  its own log queries, and a small model will happily report *that* commit as
  the root cause. This was observed: one run blamed the restore and cited a
  timestamp matching no log line at all, at 0.95 confidence.

  Three guards now push against it — `query_loki`'s `around` window is weighted
  before the fault rather than centred on it; log timestamps arrive
  pre-formatted so the model never does epoch arithmetic to quote one; and the
  system prompt carries an explicit causality rule (anything after `startsAt`
  is the fault's consequence or somebody's response, never its cause) plus a
  verbatim-evidence rule. After the fix, the same cut-then-restore-mid-analysis
  sequence produced *"the subsequent 17:30:54Z commit (session 31) is the
  restore, not the cause"*, with every cited value matching the syslog exactly.

  Treat this as reduced, not eliminated: the model is still free to
  misread evidence, and confidence scores are self-reported. A clean
  root-cause demo is still best run with the fault left in place.
- Small local models need a big enough context window for tool results, thinking
  disabled, and a low temperature, or they fail in known ways — and the gNMI tool
  must steer them to SR Linux **native** paths (not OpenConfig `/state/...`) so
  they read `oper-down-reason` and can tell an admin-disable from a fiber cut.
  See the troubleshooting runbook.
