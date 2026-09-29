# Architecture

This is a single-laptop GitOps demo of a metro fiber-ring network (SR-MPLS
by design, IS-IS/IPv4 in the lab; see [Trade-offs / scope](#trade-offs--scope))
with multi-source telemetry and event-driven impact analysis. The fictional
operator is **Atlas DOT, Region 7 — Atlanta Metro**.

The point is to show how the *operational story* — alert fires, impact is
analyzed, the right humans get paged with structured context — falls out
of standard CNCF tools when they're wired together carefully.

## Topology

```
spec/atlanta.yaml   ──►   tools/render/   ──►   workloads/{topology,gnmic,...}
```

`spec/atlanta.yaml` is the single source of truth: 12 nodes, 15 links, 11
agencies. The Go renderer (`tools/render/`, one dependency: `yaml.v3`) emits
everything topology-shaped downstream — SR Linux startup configs, FRR daemon
configs, the gNMIc target list, the Clabernetes Topology CR, the NetBox seed
JSON, link-membership and link-rate recording rules, the SNMP Probe,
snmpd.conf, the dom-synth links file, the Geomap dashboard, the
WorkflowTemplates that carry node names or credentials, pinned image versions,
and the console's target allowlist. The full list is under
[Re-rendering from spec](#re-rendering-from-spec).

Run `make render` after editing the spec. Nothing that encodes the topology
is hand-authored; the hand-maintained parts (eventing Python, Sensors and
EventSources, most dashboards, Helm values) are listed in
[CONTRIBUTING.md](../CONTRIBUTING.md).

### Roles

| Role | Count | Kind | Notes |
|---|---|---|---|
| `tmc` (Traffic Mgmt Center) | 2 | SR Linux | Backbone routers, run iBGP between TMCs |
| `corridor-hub` | 6 | SR Linux | Ring routers, terminate the FOC ring + fan out to cabinets |
| `field-cabinet` | 4 | FRR (linux kind) | "Legacy edge" — eBGP to corridor-hub, only SNMP for telemetry |

The 11 backbone links are a 6-link I-285 perimeter ring across the 6
corridor hubs, 4 TMC uplinks (tmc-1 → hub-nw and hub-sw; tmc-2 → hub-n and
hub-e), and a direct tmc-1 ↔ tmc-2 interconnect. The 4 cabinet links each
attach an FRR cabinet to one corridor hub (hub-n, hub-nw, hub-i20e,
hub-sw); every cabinet is single-homed.

## Layered architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  GitOps (ArgoCD root → workloads + platform Apps)                      │
└──────────┬─────────────────────────────────────────────────┬───────────┘
           ▼                                                 ▼
  ┌──────────────────┐                             ┌─────────────────────┐
  │  Topology layer  │                             │  Platform layer     │
  │  clabernetes:    │                             │  cert-manager       │
  │   8× SR Linux    │                             │  CNPG operator      │
  │   4× FRR         │                             │  valkey-helm        │
  │   in DinD pods   │                             │  kube-prometheus-   │
  └──────────┬───────┘                             │   stack, Loki, Alloy│
             │                                     │  argo-{events,wf}   │
             │ gNMI :57400                         │  clabernetes mgr    │
             │ SNMPv2c :161                        └─────────────────────┘
             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Telemetry plane                                                 │
  │   gNMIc       : 8 SR Linux targets, prom :9804, processors map   │
  │                 enum strings (oper-state up/down) → ints (1/2)   │
  │   snmp_exporter Probe : 4 FRR cabinets, ifMib walk               │
  │   dom-synth   : synthetic transceiver metrics per backbone port  │
  │   Alloy       : pod log scraper → Loki                           │
  └────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Prometheus + Loki                                               │
  │   - PromRule SRLInterfaceOperDown (gnmi-driven)                  │
  │   - PromRule CabinetInterfaceOperDown (snmp-driven)              │
  │   - Recording rules: link_membership_info, device_geo_info,      │
  │                      link_geo_segment, link_endpoint_geo         │
  │   - AlertmanagerConfig srl-routes  → argo-events webhook         │
  └────────────────────────────┬─────────────────────────────────────┘
                               │ webhook POST /alert
                               ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Eventing plane (argo-events + argo-workflows)                   │
  │   EventSource webhook  → JetStream EventBus                      │
  │   Sensor interface-down → enriched-notify WorkflowTemplate       │
  │   Workflow steps:                                                │
  │     enrich     — NetBox lookup (token from secretKeyRef)         │
  │     analyze    — cable graph walk → downstream + agencies        │
  │     notify     — Block Kit → Slack (or stderr if no creds)       │
  │     dashboard  — per-incident Grafana dashboard (ConfigMap)      │
  │     postmortem — Markdown report, on resolve only                │
  │   Valkey   : per-fingerprint incident ledger (24h TTL)           │
  └──────────────────────────────────────────────────────────────────┘
```

### Why each layer is here

| Layer | Why |
|---|---|
| **GitOps** | The whole thing is a story about *infrastructure as data*. ArgoCD watches `argocd/` and reconciles each workload Application. |
| **Topology** | Clabernetes runs lab nodes as nested docker containers per pod, so you can stand up multi-vendor topologies with kubernetes scheduling. |
| **Telemetry — gNMI** | Modern, streaming, model-driven — what an operator buying SR Linux today would use. |
| **Telemetry — SNMP** | The legacy edge story. Cabinets aren't SR Linux; they're FRR boxes that only speak SNMP. The same alert pipeline carries both. |
| **Telemetry — DOM** | Synthetic transceiver metrics (no real SFPs in clabernetes). Lets dashboards show optical health without faking the entire LLDP/optical YANG. |
| **NetBox** | Operational source of truth. Seed is generated from the spec — same data, different lens. The workflow's `enrich` step uses NetBox so the alert payload includes site/agency/cable_label without operator memory. |
| **Argo Events + Workflows** | Decouples "alert fired" from "someone got paged". Lets the demo show enrichment, analysis, and conditional Slack messaging in steps you can read. |
| **Loki** | All workflow output flows to Loki by default. The Alert console shows the steps live, no extra plumbing. |

### Advisory AI lanes (optional)

Two read-only AI consumers sit on top of the planes above; both are
Pydantic-AI agents over the same tool layer (PromQL, LogQL, NetBox GET),
both read the optional `ai-analyst` Secret, and neither can change the
network — the deterministic pipeline never depends on them.

- **Incident analyst** ([ai-analyst.md](ai-analyst.md)) — event-triggered: its own
  Sensor fires the `ai-analyst` WorkflowTemplate per alert, in parallel
  with the paging lane. Output is a structured analysis threaded under the
  Slack incident card and folded into the postmortem.
- **Console chat** ([chat.md](chat.md)) — interactive: a long-lived
  `chat-agent` Deployment behind the console's `/api/chat` ingress path
  streams answers into the scenario console's "Ask the network" panel.
  Demo-critical questions ride deterministic tools — `corridor_impact`
  (NetBox cable-graph reachability walk) and `firing_alerts` (the status
  tile's exact `ALERTS` query) — so blast-radius and alert answers are
  computed, never guessed.

## Telemetry — the gNMI / SNMP split

The fictional Atlas DOT runs a mixed fleet: modern SR Linux backbone, FRR
cabinets at the edge that haven't been refreshed yet. Same demo, two
telemetry pipelines:

```
SR Linux  ──► gNMI subscribe (5s/10s/60s tiers) ──────────► gNMIc :9804
                                                              │
FRR/Linux ──► SNMP poll every 300s ─────► snmp_exporter ──────┼──► Prometheus
                                                              │
synthetic ──► dom-synth (Python HTTP)                ─────────┘

ServiceMonitor metricRelabelings on gnmic project source / interface_name
into node / interface so the dashboards and link_membership_info join key
work for *both* pipelines without per-pipeline expressions.
```

`event-strings` + `event-convert` processors in gnmic map SR Linux's enum
strings (`up`, `down`, `enable`, `disable`) to ints (1, 2, 1, 0) so the
prometheus output isn't dropped (gnmic discards non-numeric values).

## Eventing — alert to Slack in five hops

```
Prometheus alert (firing)
  → AlertmanagerConfig srl-routes   (route by namespace/severity)
    → argo-events EventSource webhook  POST /alert
      → JetStream EventBus
        → Sensor interface-down (filter: alertname matches list)
          → Workflow enriched-notify
              ├─ enrich     : NetBox lookup
              ├─ analyze    : cable graph + agency mapping + severity
              ├─ notify     : Block Kit Slack (or stderr if no creds)
              │                + Valkey ledger update (resolve closes the thread)
              ├─ dashboard  : create (firing) / delete (resolved) the
              │                per-incident Grafana dashboard
              └─ postmortem : on resolve, write the Markdown report to Valkey
```

Three properties make this work in a demo setting:

1. **Alert gating uses `link_membership_info`**, not `admin_state == 1`.
   `make demo-cut` admin-disables an interface, which would defeat an
   admin-state gate. Joining the alert expression against
   `link_membership_info` filters out the unused IXR-D3 ports (34 per
   node, 26 cabled across the 8 nodes, so ~246 unused) without filtering
   out the cut interface.

2. **The alert payload reaches the workflow as an env var, not as inline
   Python source.** Argo's parameter substitution into a triple-quoted
   Python string makes Python interpret `\n` as a real newline, breaking
   `json.loads`. As an env value, the substitution stays at the
   YAML-scalar layer.

3. **The NetBox token is mirrored into a Secret by the seed Job.** NetBox
   4.x stores tokens hashed; `tokens/provision/` returns a fresh
   plaintext per call, so a hardcoded value would always 403. The
   Workflow reads `argo-events/netbox-api` via `secretKeyRef`, populated
   by `seed.py` after each successful provision.

## GitOps shape

```
argocd/
├── applicationset.yaml        # generates one Application per stub below
└── manifests/
    ├── platform/    # third-party charts (kps, loki, alloy, argo-{events,wf}, ...)
    └── workloads/   # this-repo manifests (topology, gnmic, observability, snmp,
                     #   eventing, netbox*, dom-synth, incident-dashboards,
                     #   chat-agent, console)
```

`bootstrap/root-app.yaml` is the `root` Application; it applies the
ApplicationSet, whose `git` files generator globs both
`manifests/platform/*.yaml` and `manifests/workloads/*.yaml` and templates one
Application per stub (21 today, so `kubectl -n argocd get applications`
shows 22 rows including `root`). Each owns one logical chunk. The `topology`, `gnmic`, and `snmp` stubs
carry `ignoreDifferences` blocks so clabernetes' admission webhook defaults and
the Prometheus operator's stored `action: replace` don't register as drift. All
stubs auto-sync (prune + selfHeal).

## Trade-offs / scope

- **No real SR-MPLS.** The public `ghcr.io/nokia/srlinux` image doesn't
  advertise the `mpls` or `segment-routing` base features on any 7220
  IXR chassis; the YANG containers are `if-feature`-gated. Lab runs IS-IS
  / IPv4 only. The narrative still positions the topology as SR-MPLS in
  *design* — runtime forwarding is plain L3.
- **No real SFPs.** Optical metrics are synthetic (see `dom-synth`).
- **No real Slack.** `notify.py` short-circuits to stderr when the
  `slack-bot` Secret is absent (`make last-notify` shows the payload). See
  [SECRETS.md](../SECRETS.md).
- **Single-laptop scale.** k3d, 1 server + 2 agents. CNPG is a single
  Postgres pod; Loki is SingleBinary; Prometheus is 6h retention. None
  of this is HA. It's all on purpose.

## Sync waves

The ApplicationSet creates all Applications at once. Each stub's `syncWave`
becomes the `argocd.argoproj.io/sync-wave` annotation on its Application,
which records the *logical* dependency order; it is not an enforced
sequence. An app whose dependencies aren't ready yet simply stays
Progressing until a later reconcile converges it.

| Wave | Components |
|---|---|
| `-1` | cert-manager |
| `0` | CNPG operator, Clabernetes operator, kube-prometheus-stack, Loki, Argo Workflows, Argo Events |
| `1` | Valkey, Alloy, topology |
| `2` | NetBox prereqs (ClusterIssuer + CNPG Cluster), gNMIc, SNMP exporter, dom-synth, observability rules, incident-dashboards |
| `3` | NetBox chart, eventing CRs (EventBus + EventSource + Sensors + WorkflowTemplates), chat-agent, console |
| `4` | NetBox seed Job |

Source: the `syncWave` field in each `argocd/manifests/{platform,workloads}/*.yaml`.

## Re-rendering from spec

Edit `spec/atlanta.yaml` and run `make render`. The renderer
(`tools/render/main.go`) rewrites:

- `workloads/topology/startup-configs/*`: per-node SR Linux `.cfg` and FRR
  `.frr` configs, the shared FRR `daemons` file, and the shared
  `snmpd.conf` + `wrapper.sh` for the legacy-edge lane
- `workloads/topology/topology.yaml` + `kustomization.yaml`
- `workloads/gnmic/targets.yaml`
- `workloads/netbox/seed/seed.json`
- `workloads/observability/link-membership.yaml` (link / device / endpoint
  recording rules) and `link-rate-rules.yaml`
- `workloads/observability/dashboards/geomap.json`, plus the cross-dashboard
  navigation links in every dashboard in that directory
- `workloads/snmp/probe.yaml`
- `workloads/dom-synth/links.json` (synthetic transceiver exporter feed)
- `workloads/eventing/wft-{cut-fiber,incident-collector,enriched-notify,maintenance,remediation,drift-audit,ai-analyst,gray-failure}.yaml`
  and `workloads/eventing/scripts/drift_expected.json`
- `workloads/versions.yaml` (pinned upstream image versions)
- `tools/console/static/console-targets.json` (the console's allowlist)

Commit the diff and push; Argo CD syncs from Git. `make render-check` (and
CI) fail if any of these drift from a fresh render; see
[CONTRIBUTING.md](../CONTRIBUTING.md#the-one-rule-a-lot-of-files-are-generated--dont-hand-edit-them).

## Repository layout

```text
spec/atlanta.yaml          single source of truth (12 nodes, 15 links, 11 agencies)
tools/
  render/                  Go renderer: spec → configs, targets, seed, rules, WFTs, geomap
  console/                 scenario console (Go) + its static UI
  gifgen/                  Playwright recorder behind bin/make-gif.sh
bin/                       ready gate, scenarios, maintenance, measurement, GIF scripts
images/                    Dockerfiles for the pre-baked images (make build)
k3d/config.yaml            cluster shape, port maps, in-cluster registry
bootstrap/                 argocd-install.sh + root-app.yaml (the root Application)
argocd/                    applicationset.yaml + manifests/{platform,workloads}/ per-app stubs
platform/values/           Helm values for each platform chart
workloads/
  netbox/                  NetBox chart values + CNPG Cluster + seed Job
  topology/                Clabernetes Topology CR + startup-config bundle
                             (SR Linux .cfg, FRR .frr, daemons, snmpd.conf,
                             wrapper.sh entrypoint)
  gnmic/                   gNMIc Deployment + ServiceMonitor (modern lane)
  snmp/                    snmp_exporter + Probe CR + PromRule (legacy lane)
  dom-synth/               synthetic transceiver (DOM) exporter
  observability/           PromRules + AlertmanagerConfig + dashboards
  eventing/                EventSource + Sensors + WorkflowTemplates +
                             Python step scripts (enrich/analyze/notify/…)
  incident-dashboards/     namespace + RBAC for per-incident dashboards
  chat-agent/              "Ask the network" chat backend
  console/                 scenario console Deployment + ingress
  versions.yaml            pinned upstream image versions (rendered)
docs/                      architecture, runbooks, AI docs, images in docs/assets/
results/                   measured latency results (RESULTS-SUMMARY.md)
Makefile                   make doctor | demo | up | down | status | urls | render | demo-cut | …
```
