# Demo runbook

A repeatable script for running the live demo end-to-end. Assumes the
reader has already followed the README to bring `atlas-demo` up.

## Pre-demo checklist

Run all of these before you stand in front of an audience.

```bash
# 1. cluster + apps healthy
kubectl -n argocd get applications --no-headers | awk '$2!="Synced" || $3!="Healthy"'
# expected: empty — all 21 Applications auto-sync to Synced/Healthy.
# (netbox-seed shows Progressing for a minute while its seed Job runs.)

# 2. all 12 lab pods ready
kubectl -n clabernetes get pods | awk '/atlanta/ && !/1\/1.*Running/'
# expected: empty output

# 3. IS-IS converged (each SR Linux node has 3 adjacencies)
for n in tmc-1 tmc-2 hub-n hub-e hub-i20e hub-nw hub-sw hub-i20w; do
  P=$(kubectl -n clabernetes get pods -l clabernetes/topologyOwner=atlanta,clabernetes/topologyNode=$n -o jsonpath='{.items[0].metadata.name}')
  CNT=$(kubectl -n clabernetes exec "$P" -- docker exec $n bash -c "echo 'show network-instance default protocols isis adjacency' | sr_cli" 2>/dev/null | grep -c "| up ")
  printf "%-12s %s adjacencies\n" "$n" "$CNT"
done
# expected: tmc-1=3 tmc-2=3 hub-* = 3 each

# 4. gnmic emits oper_state metrics
kubectl -n monitoring port-forward svc/gnmic 9804:9804 >/dev/null 2>&1 &
sleep 2
curl -s http://127.0.0.1:9804/metrics | grep -c "^srl_nokia_interfaces_interface_oper_state{"
# expected: ~272 series (8 nodes × 34 ports, varies)

# 5. snmp probes return 200
for n in fc-n fc-nw fc-i20e fc-sw; do
  echo -n "$n: "
  kubectl -n monitoring exec deploy/snmp-exporter -- wget -qO- "http://localhost:9116/snmp?target=atlanta-$n.clabernetes.svc.cluster.local:161&module=if_mib&auth=public_v2" >/dev/null 2>&1 && echo OK || echo FAIL
done

# 6. argo-events sensor connected
kubectl -n argo-events logs -l sensor-name=interface-down --tail=5 | grep -i "started subscribing"
# expected: a "started subscribing" line per restart

# 7. dom-synth pumping
kubectl -n monitoring port-forward svc/dom-synth 8000:8000 >/dev/null 2>&1 &
sleep 2
curl -s http://127.0.0.1:8000/metrics | grep -c "^dom_temperature_celsius"
# expected: 26
```

## URLs to have open in tabs

| Tab | URL |
|---|---|
| Grafana — Network overview | https://grafana.127-0-0-1.nip.io/d/network-overview |
| Grafana — Atlanta metro Geomap | https://grafana.127-0-0-1.nip.io/d/geomap |
| Grafana — Device detail | https://grafana.127-0-0-1.nip.io/d/device-detail |
| Grafana — Link detail | https://grafana.127-0-0-1.nip.io/d/link-detail |
| Grafana — Alert console | https://grafana.127-0-0-1.nip.io/d/alert-console |
| ArgoCD | https://localhost:8443 (port-forward 8080:443 from argocd-server) |
| NetBox | https://netbox.127-0-0-1.nip.io |
| Argo Workflows UI | https://workflows.127-0-0-1.nip.io |
| clabernetes UI | https://clabernetes.127-0-0-1.nip.io |

## The demo (≈10 minutes)

### Act 1 — set the stage (≈2 min)

Open **Geomap** first.

> Atlas DOT runs a metro fiber network across Atlanta — eight SR Linux
> backbone routers, a closed FOC ring, four legacy field cabinets at the
> edge running FRR. All twelve sites here are real Atlanta neighborhoods.

Click into **Network overview**.

> Top bar: 8 nodes UP, 11 backbone links lit, 4 cabinets reachable, 0
> critical alerts. Aggregate egress is the math homework.
>
> The configured-links table is interesting because clicking a row drills
> in — a Link cell goes to the link-detail dashboard, a Device cell goes
> to device-detail.

Click into **Device detail** for one of the corridor hubs.

> This is per-device. All its interfaces, oper-state stepper, traffic in
> and out (in is negated so it mirrors), errors and discards, and at the
> bottom — transceiver case temperature and Rx/Tx optical power per
> port.

### Act 2 — cut the fiber (≈3 min)

> Watch the Geomap. I'm going to admin-disable a single interface — same
> effect on neighbours as a fiber cut. The peer end is still cabled, the
> link goes oper-down on both sides.

```bash
make demo-cut NODE=hub-i20e INTERFACE=ethernet-1/2
```

> Twenty seconds later — Prometheus picks up oper_state=2. The alert is
> in pending. Thirty seconds after that — firing.

Switch to **Alert console**.

> One row appears, severity-coded. Notice the Link column already has
> the link_id — `ring-e-i20e` — and the Kind column says `backbone`.
> That came from the recording rule join, not from the alert template.

Switch to the **Argo Workflows UI**.

> A new `enrich-notify-XXXXX` workflow ran. Click it.
>
> Three steps. The `enrich` step hit NetBox — site, agency, cable
> label. The `analyze` step walked the cable graph from there:
> hub-i20e is a corridor hub, so taking it down isolates its
> field cabinet (fc-i20e) AND removes one ring segment.
> Severity: high.
>
> The `notify` step would post to Slack — for this demo it's
> short-circuited to stderr because we deliberately don't ship a Slack
> token in the public repo.

### Act 3 — restore + close (≈2 min)

```bash
make demo-restore NODE=hub-i20e INTERFACE=ethernet-1/2
```

> Within a minute the alert clears. The line on the Geomap goes green
> again. If we'd posted to Slack, you'd now see the original message
> updated with a ✅ + downtime computed from `alert.startsAt → endsAt`,
> plus a threaded resolution summary. The Valkey ledger key gets DEL'd.

### Pre-canned outage scenarios

For deeper / hands-free walkthroughs, the canned scenarios run a script
of cuts + sleeps + restores. Each auto-restores on completion or on
Ctrl-C.

```bash
make scenario-list             # show what's available
make scenario-hurricane        # 2 ring segments fail in series, ~2.5 min
make scenario-backhoe          # one random backbone strand cut for ~2 min
make scenario-cabinet          # field cabinet uplink failure, ~1.5 min
make scenario-flap             # trip SRLInterfaceFlapping via rapid up/down
```

`hurricane` is the headline — drop two ring segments thirty seconds
apart so the audience sees the analyze step flag fc-i20e as stranded
*because* the second cut isolated it, then watch the recovery roll back
in reverse.

### Act 4 (optional, advanced) — the legacy edge (≈3 min)

Same demo, on the legacy edge: a real carrier loss on a cabinet uplink (the
interface goes oper-down while admin-state stays up — a link failure, not a
maintenance shutdown), detected by SNMP polling instead of streaming gNMI.

```bash
make demo-cut-cabinet NODE=fc-n INTERFACE=eth1
```

> No SR Linux involvement here. The cabinet is FRR; its only telemetry
> is SNMPv2c on udp:161. The same workflow fires — the demo shows that
> mixed-vendor fleets don't have to rip-and-replace to get unified
> alerting and enrichment.

```bash
make demo-restore-cabinet NODE=fc-n INTERFACE=eth1
```

## Optional Slack hook-up

If you want real Slack messages instead of stderr:

```bash
kubectl -n argo-events create secret generic slack-bot \
  --from-literal=bot_token='xoxb-...' \
  --from-literal=channel_id='C0123456789'
```

The next workflow run picks it up via `secretKeyRef.optional: true`.
Resolution updates the original message and threads a summary.

## Soft reset

If anything's stuck mid-demo, the cleanest reset is:

```bash
make demo-restore NODE=<X> INTERFACE=<Y>      # un-cut
kubectl -n argo-events delete wf --all        # drop in-flight workflows
kubectl -n argo-events get sensors interface-down -o yaml | \
  kubectl apply -f -                          # re-arm sensor (occasionally needed)
```

Hard reset (re-creates everything from git): see
`docs/runbook-troubleshoot.md`.

## Gray-failure smoke test

The `gray-failure` scenario simulates a deteriorating optic + climbing
ingress error rate on a chosen backbone link, exercising the
warning-severity branch of the enriched-notify pipeline. It auto-recovers.

1. Pick a backbone link from `workloads/dom-synth/links.json` (e.g. `ring-n-e`).

2. Start the scenario (default 600s, 8 dBm Rx offset, 120 err/s peak):

   ```bash
   make scenario-gray-failure LINK=ring-n-e
   ```

   Or shorter for a quick smoke:

   ```bash
   SCENARIO_DURATION=300 make scenario-gray-failure LINK=ring-n-e
   ```

3. Verify the control key landed:

   ```bash
   kubectl -n valkey exec valkey-0 -- valkey-cli -n 3 GET gray:ring-n-e
   ```

4. In Grafana → **device-detail** → pick `hub-n` or `hub-e`. The
   `dom_rx_power_dbm` series for the affected port should ramp down
   over ~2 minutes.

5. After ~90 seconds, check Alertmanager:

   ```bash
   kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093:9093 &
   curl -s localhost:9093/api/v2/alerts | jq '.[] | select(.labels.alertname | startswith("SRL"))'
   ```

   Expected: `SRLOpticalDegrading` *firing*, `severity=warning`,
   `link_id=ring-n-e`. After another ~60 seconds, `SRLInterfaceErrorsHigh`
   also fires.

6. If Slack is wired (`slack-bot` Secret present), confirm two yellow
   Block Kit messages in the channel. Otherwise:

   ```bash
   kubectl -n argo-events logs -l workflows.argoproj.io/workflow \
     --tail=200 -c main | grep -A40 "block_kit"
   ```

7. At the end of the duration, both alerts resolve and the original
   Slack messages flip to ✅ with downtime annotation.

8. Verify the Valkey key auto-cleared:

   ```bash
   kubectl -n valkey exec valkey-0 -- valkey-cli -n 3 KEYS 'gray:*'
   ```

   Expected: empty within ~30 seconds of duration end.

### Early termination

```bash
make scenario-gray-failure-end LINK=ring-n-e
```

Within one scrape (~15 s), the exporter returns to baseline. Alerts
resolve within `for:` window (1–2 min).

### Negative cases worth probing

- Set a key with `peak_rx_offset_dbm: 0, peak_errors_per_sec: 0` →
  baseline output, no alerts fire.
- Set `duration_s: 0` → exporter logs no warning, no metrics affected
  (skipped).
- Run two scenarios on different links concurrently → independent ramps;
  alerts fire per link.

## AI incident analyst (advisory lane)

The same alert, two analyses: the deterministic pipeline's enrichment,
and — when enabled — an LLM agent that interrogates the network
read-only and publishes a structured `IncidentAnalysis`.

1. Enable the lane (optional; without it every `ai-analyze-*` workflow
   no-ops with "AI disabled"): create the `ai-analyst` Secret per
   `SECRETS.md` — any OpenAI-compatible endpoint works, including a
   local Ollama at zero cost.
2. Trigger an incident with agency impact:
   `make demo-cut NODE=hub-i20e INTERFACE=ethernet-1/4`
3. Watch the lane: `kubectl -n argo-events get workflows` — an
   `ai-analyze-*` workflow runs alongside `enrich-notify-*`, never
   blocking it. Its pod log ends with one
   `INCIDENT_ANALYSIS_V1 {...}` line.
4. See it on the **Alert console** dashboard — "AI analyst —
   IncidentAnalysis (advisory lane)" panel.
5. Restore (`make demo-restore NODE=hub-i20e INTERFACE=ethernet-1/4`)
   and fetch the postmortem (`make postmortem FP=<fingerprint>`): the
   analysis appears as the "Analyst narrative (AI)" section.

The agent is advisory forever: its tools are structurally read-only
(gNMI Get-only module, allowlisted inputs) and remediation stays in the
deterministic lane.

## Per-incident dashboard (auto-generated)

Every firing alert gets its own Grafana dashboard, torn down on
resolve — look in the **Incidents** folder.

1. `make demo-cut NODE=hub-i20e INTERFACE=ethernet-1/4`
2. Within ~30s of the alert firing, Grafana grows an
   `INCIDENT — SRLInterfaceOperDown on hub-i20e (<fp>)` dashboard:
   incident context (cable/SLA/agencies), link state + traffic, a
   downstream-health grid (SRL oper-state and cabinet SNMP
   reachability — watch whether the ring redundancy held), the live
   AI analysis (self-populates when the advisory lane finishes), and
   the device log stream.
3. `make demo-restore NODE=hub-i20e INTERFACE=ethernet-1/4` — when the
   resolve processes, the dashboard disappears.
