# Measured results — detection & enrichment latency

Environment: single-host k3d (kind-equivalent) on Apple Silicon (arm64). All
timestamps are exact cluster-object times (Prometheus `activeAt`, Argo workflow
`finishedAt`) differenced against the cut-issue wall clock; every container
shares the host kernel clock. Absolute values are environment-bound — the
defensible claim is the **streaming-vs-polling delta** and the **lane-independent
enrichment**, both of which are structural.

## 1. Link-down detection — streaming (gNMI) vs polling (SNMP)

| Metric | gNMI / streaming (N=20) | SNMP / 5-min polling (N=4) |
|---|---|---|
| Detection latency (median) | **18.2 s** | **86 s** |
| Detection latency (mean / max) | 19.6 / 44.2 s | 142 / 315 s |
| Alert fires (median, incl. debounce) | 18.2 s (`for: 0s`) | 146 s (`for: 1m`) |
| Enrichment (analyze→notify) | **30.8 s (constant)** | **30.7 s (constant)** |
| End-to-end to enriched notification (median) | 49 s | 177 s |

- Streaming detection is **eval-bound** (~18 s = the 30 s Prometheus rule-eval grid + 15 s scrape; the 5 s telemetry sample is not the limiter). Do **not** claim ~5 s.
- Polling detection is a **uniform distribution bounded by the poll interval** (cut lands anywhere in the 5-min window → 78–315 s observed), plus the 60 s debounce.
- **Headline delta: ≈5× at the medians (18 s vs 86 s), up to ~17× at the tails** (18 s vs 315 s).
- **Enrichment is identical across lanes (~30 s).** This is the empirical backbone of the thesis: the model-driven *understanding* is constant regardless of how the signal arrived — the lane difference is entirely in *detection*.

## 2. Gray-failure detectability — streaming vs polling vs traps

Controlled optical degradation (`dom_rx_power_dbm < −12`, `for: 2m`); polling
counterfactual derived by decimating the captured high-resolution signal to a
300 s sampler across all phases; traps = none (no SNMP trap exists for a
gauge/counter crossing).

| Failure duration | Streaming | 5-min polling | Traps |
|---|---|---|---|
| 180 s (dwell 150 s) | ✅ fires 141 s | **55% detect** | **0%** |
| 360 s (dwell 315 s) | ✅ fires 146 s | 100% (~292 s) | **0%** |
| 600 s (dwell 555 s) | ✅ fires 141 s | 100% (~292 s) | **0%** |

- Streaming catches **every** gray failure (signal seen ~20 s; alert after the 2-min debounce ≈ 140 s).
- 5-min polling is **probabilistic** for sub-poll failures (55% at 180 s) and merely slower for longer ones.
- **Traps detect gray failures 0% of the time** — categorically, not just slowly. This is the strongest single figure: a rising-error/optical degradation has no `linkDown`-style trap, so the legacy edge is blind to it regardless of latency.

## Three claims these support

1. **Detection: streaming ≈5–17× faster than realistic 5-min SNMP polling** (measured, not asserted) — and faster *and* reliable for transients/gray failures where polling aliases and traps are blind.
2. **Understanding is the bottleneck, and it's lane-independent (~30 s constant).** Faster detection alone isn't the contribution; the model-driven enrichment that turns a bare signal into a contextual, actionable incident is — and it costs the same regardless of telemetry source.
3. **The legacy edge is categorically blind to gray failures** (traps 0%, 5-min polling probabilistic) — only continuous streaming represents them at all.
