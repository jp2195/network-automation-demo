# network-automation-demo

A self-contained, GitOps-managed Kubernetes demo of streaming-telemetry-driven
incident response over an SR-MPLS topology. Runs on a single laptop. Every
artifact — SR Linux configs, FRR configs, gNMIc targets, NetBox seed,
Grafana GeoJSON, Clabernetes Topology CR, Prometheus recording rules — is
generated from one source-of-truth file: [`spec/atlanta.yaml`](./spec/atlanta.yaml).

The headline isn't "we wired up Slack." It's: a generic interface-down
alert gets enriched with NetBox context (cable, corridor, providers,
agencies), analyzed for downstream impact, and turned into an actionable
Slack message that **updates in place when the alert resolves** — driven by
a 3-step Argo Workflow with an alert-fingerprint-keyed ledger in Valkey.

## Topology

12 nodes, fictional Atlas DOT Region 7 Atlanta Metro:

- 2 Transportation Management Centers (TMC) — SR Linux backbone heads
- 6 corridor hub aggregators — SR Linux on the I-285 / I-75 / I-20 / GA-400 corridors
- 4 field-cabinet routers — FRR (Linux) eBGP into the SR-MPLS core

15 links total: an I-285 perimeter ring, TMC redundant uplinks, and per-cabinet drops.

## Stack

| Layer | Component |
|---|---|
| Cluster | k3d (k3s in Docker), Traefik ingress, local-path storage |
| GitOps | ArgoCD (App-of-Apps from `argocd/`) |
| Topology | Clabernetes operator + containerlab-flavored `Topology` CR |
| Source of truth | NetBox (CNPG Postgres + valkey-io Valkey, no Bitnami workloads) |
| Telemetry — modern | gNMIc streaming subscriptions on the SR Linux backbone → Prometheus |
| Telemetry — legacy | Prometheus `snmp_exporter` polling FRR cabinets (IF-MIB) |
| Logs | Alloy DaemonSet → Loki SingleBinary |
| Eventing | Argo Events (NATS JetStream EventBus) → Argo Workflows |
| Notifications | Slack (`slack-sdk` bot, `chat.update` on resolve) |
| Certs | cert-manager (selfsigned ClusterIssuer, traefik ingresses on `*.127-0-0-1.nip.io`) |

Working set ≈ 25 GB on a 32 GB+ laptop. ARM64 hosts work — SR Linux
(`ghcr.io/nokia/srlinux`) is multi-arch.

## Telemetry sources — the legacy / modern split

The 8 SR Linux backbone nodes stream telemetry via gNMI to gNMIc,
which exposes it as Prometheus metrics (`srl_*`, refreshed every 5–60 s
across five tiered subscription groups: `if-state`, `if-counters`,
`transceiver`, `routing`, `system`). This is the modern lane —
push-based, schema-defined, sub-second detection latency.

The 4 FRR field cabinets are deliberately *not* on that pipeline. Each
cabinet runs a tiny `snmpd` (installed on first boot via
`apk add net-snmp` in the entrypoint wrapper) listening for SNMPv2c on
UDP/161. A `prom/snmp_exporter` deployment polls each cabinet every
30 s for the standard `IF-MIB` tables, and a Prometheus-Operator
`Probe` CR registers them with kube-prometheus-stack.

The point: the rest of the demo — Alertmanager → EventSource → Sensor
→ enriched-notify Workflow → Slack — is **identical** for both
telemetry sources. The legacy edge and the modern core land at the same
incident response flow, with the same NetBox enrichment and impact
analysis. Mixed legacy/modern fleets don't have to rip-and-replace to
get modern incident response — that decoupling is the operational
takeaway.

| Lane | Nodes | Collection | Sample rate | Metric prefix |
|---|---|---|---|---|
| Modern | 8 SR Linux backbone | gNMI streaming → gNMIc | 5 s state / 10 s counters / 30 s DOM / 60 s system | `srl_*` |
| Legacy | 4 FRR field cabinets | SNMPv2c polling → snmp_exporter | 30 s | `ifOperStatus`, `ifInOctets`, … |

## Prerequisites

- Docker (or [OrbStack](https://orbstack.dev) on macOS)
- [`k3d`](https://k3d.io) ≥ v5.6
- `kubectl`
- `helm`
- `make`
- `go` 1.22+ (only if you re-render from spec)

## Quickstart

> **Push first.** Every ArgoCD `Application` references
> `https://github.com/jp2195/network-automation-demo.git` on branch `main`.
> Push this repo to that remote — or rewrite `repoURL` across `argocd/` and
> `bootstrap/root-app.yaml` to wherever you've put it — before `make up`.

```bash
make up        # creates k3d, installs ArgoCD, applies the App-of-Apps root
make status    # nodes + ArgoCD app state + ArgoCD URL/admin password
make down      # tear the cluster down
make render    # re-render workloads/* from spec/atlanta.yaml
```

UIs after sync settles:

| URL | Notes |
|---|---|
| <http://argocd.127-0-0-1.nip.io:8080> | admin / `make status` shows password |
| <https://netbox.127-0-0-1.nip.io:8443> | admin/admin (selfsigned TLS) |
| <https://grafana.127-0-0-1.nip.io:8443> | admin/admin |
| <https://workflows.127-0-0-1.nip.io:8443> | server-mode, no auth |
| <https://clabernetes.127-0-0-1.nip.io:8443> | clabernetes UI |

## Demo flow

Once IS-IS has converged across the 8 SR Linux backbone nodes and
snmp_exporter is reaching all 4 cabinets:

**Modern lane (gNMI / SR Linux):**

```bash
# Disable an interface via SR Linux CLI — gNMIc sees oper-status DOWN,
# Prometheus rule fires, Alertmanager webhooks the EventSource, Sensor
# triggers the enriched-notify Workflow, Slack gets a Block Kit message.
make demo-cut     NODE=tmc-1 INTERFACE=ethernet-1/1

# Re-enable it — same alert fingerprint resolves; the original Slack
# message is updated in place to RESOLVED with downtime, and a thread
# reply summarizes which downstream cabinets/agencies are restored.
make demo-restore NODE=tmc-1 INTERFACE=ethernet-1/1
```

**Legacy lane (SNMP / FRR cabinet):**

```bash
# Same enrich/analyze/notify pipeline, but the alert is sourced from
# snmp_exporter polling the cabinet's snmpd rather than streaming gNMI.
make demo-cut-cabinet     NODE=fc-n INTERFACE=eth1
make demo-restore-cabinet NODE=fc-n INTERFACE=eth1
```

The 3-step DAG:

1. **enrich** — NetBox lookup: device → site → primary IP → interface →
   cable → custom fields (corridor, provider, SLA, route description).
2. **analyze** — walk the cable graph from the affected device to find
   downstream cabinets, the agency tenants on each, and a
   `severity_class` (high if a cabinet is impacted, medium if multiple
   downstream devices, else low).
3. **notify** — branches on `alert.status`:
   - `firing`: `chat.postMessage` Block Kit; persist
     `{ts, channel, first_seen, impact}` in Valkey under
     `incident:<fingerprint>` with a 24h TTL.
   - `resolved`: load the ledger, `chat.update` the original message
     in place (✅ + downtime), thread reply with the resolution
     summary, DEL the ledger key.

## Slack

Without real Slack credentials the workflow's notify step prints the
Block Kit payload to stderr instead of calling the API — visible via
`kubectl logs` on the workflow step pod.

**To enable real posting without committing your bot token to git**, see
[`SECRETS.md`](./SECRETS.md) for two override patterns (hand-applied
`secrets.local/slack-bot.yaml`, or sealed-secrets for git-stored
encrypted secrets).

## Layout

```
spec/atlanta.yaml          single source of truth (12 nodes, 15 links, 11 agencies)
tools/render/              Go renderer: spec → SRL/FRR configs, gNMIc targets,
                             NetBox seed, GeoJSON, Topology CR,
                             link_membership PromRule
k3d/config.yaml            cluster shape, port maps, in-cluster registry
bootstrap/                 manual one-shot: argocd-install.sh + root-app.yaml
argocd/{platform,workloads}/  one ArgoCD Application per chart / kustomize dir
platform/values/           helm values for each platform chart
workloads/
  netbox/                    netbox-chart values + CNPG Cluster + seed Job
  topology/                  Clabernetes Topology CR + startup-config bundle
                               (SR Linux .cfg, FRR .frr, daemons, snmpd.conf,
                               wrapper.sh entrypoint)
  gnmic/                     gNMIc Deployment + ServiceMonitor (modern lane)
  snmp/                      snmp_exporter + Probe CR + PromRule (legacy lane)
  observability/             PromRules + AlertmanagerConfig + dashboards
  eventing/                  EventSource + Sensors + WorkflowTemplates +
                               Python step scripts (enrich/analyze/notify)
Makefile                   make up | down | status | render | demo-cut | demo-restore
SECRETS.md                 how to use real credentials without committing them
```

## Sync waves

| Wave | Components |
|---|---|
| `-1` | cert-manager |
| `0` | CNPG operator, Clabernetes operator, kube-prometheus-stack, Loki, argo-workflows, argo-events |
| `1` | Valkey, Alloy, Topology |
| `2` | NetBox prereqs (ClusterIssuer + CNPG Cluster), gNMIc, observability rules |
| `3` | NetBox chart, eventing CRs (EventBus + EventSource + Sensors + WFTs) |
| `4` | NetBox seed Job |

## Re-rendering from spec

Edit `spec/atlanta.yaml` and run `make render`. The renderer rewrites:

- `workloads/topology/startup-configs/*` (per-node SR Linux + FRR configs,
  shared `daemons` for FRR, shared `snmpd.conf` + `wrapper.sh` for the
  legacy-edge lane)
- `workloads/topology/topology.yaml` + `kustomization.yaml`
- `workloads/gnmic/targets.yaml`
- `workloads/netbox/seed/seed.json`
- `workloads/observability/dashboards/links.geojson`
- `workloads/observability/link-membership.yaml` (link / device / endpoint
  recording rules)
- `workloads/dom-synth/links.json` (synthetic transceiver exporter feed)

Commit the diff. ArgoCD syncs.

## More docs

- [`docs/architecture.md`](docs/architecture.md) — layered architecture,
  why each piece is here, the gNMI / SNMP / DOM split, eventing flow.
- [`docs/runbook-demo.md`](docs/runbook-demo.md) — pre-demo checklist,
  the live demo script (≈10 min), optional Slack hook-up.
- [`docs/runbook-troubleshoot.md`](docs/runbook-troubleshoot.md) —
  symptom → diagnosis lookup table, subtle gotchas, hard reset.
