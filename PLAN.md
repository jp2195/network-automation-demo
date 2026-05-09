# SR-MPLS Demo Stack — Plan

A self-contained Kubernetes demo that runs on a single laptop (Mac or Linux), driven entirely by GitOps, demonstrating modern streaming-telemetry-driven incident response over an SR-MPLS topology.

---

## 1. Goal

End-to-end demo loop on one machine, with a Georgia DOT / ATMS flavor: fiber rings along corridors (I-75, I-285, I-20, GA-400), Transportation Management Center (TMC) at the head end, six field aggregation hubs along major interchanges, four field-cabinet simulators standing in for the ITS edge, agencies and ITS devices as the real-world consumers. Same architecture you'd build for a real DOT customer engagement.

**Target tier: District-rich** (12 nodes total — 8 SR Linux for the backbone, 4 FRR for field cabinets). Working set ~20 GB. Comfortably fits a 64 GB laptop. Demonstrates everything the Compact tier does *plus* end-to-end service paths from TMC through hubs out to the ITS edge — the dashboards show real device counts behind each hub, and the enrichment workflow can name specific cabinets affected by a fiber cut.

```
SR Linux topology (Clabernetes)
        │  gNMI dial-in subscriptions
        ▼
     gNMIc  ──► Prometheus ──► Alertmanager
                                    │ webhook
                                    ▼
                             Argo Events (EventSource → Sensor)
                                    │ trigger
                                    ▼
                             Argo Workflow
                                    │ enrich (NetBox)
                                    │ collect DOM (gNMI both ends)
                                    │ analyze (optical + impact)
                                    │ format Block Kit message
                                    ▼
                                  Slack
```

The headline isn't "we wired up Slack." It's "your source of truth + live transceiver telemetry turn a raw alert into a fiber-crew dispatch order with corridor mile markers, optical diagnosis, and per-agency impact." That's the operational maturity story DOT customers actually pay for.

Plus a representative GitOps-managed infrastructure stack (ArgoCD, NetBox on CNPG, full observability with Prometheus + Loki) so the platform pieces feel real, not contrived.

---

## 2. Cluster runtime

**k3d** (k3s in Docker). Identical UX on Mac/Linux. One config file, one command.

**Cluster shape:** 1 server + 2 agents. Agents give us a place to land workloads with anti-affinity if we want, and approximate a real cluster.

**Why not kind/minikube:** k3d ships with Traefik, ServiceLB (klipper), and local-path storage out of the box — three fewer things to install and configure. Closer to a real prod cluster than minikube.

---

## 3. Ingress / access strategy (the question you asked)

### Pattern: Traefik + nip.io wildcard DNS, no `/etc/hosts` edits

- **k3d already ships Traefik** as the default ingress controller. We use it.
- **k3d port-maps host `:8080` → cluster `:80`** and host `:8443` → cluster `:443` via the loadbalancer node. Using 8080/8443 instead of 80/443 avoids privilege issues on Linux and conflicts on Mac.
- **Hostnames use `nip.io`** so we never touch `/etc/hosts`:
  - `argocd.127-0-0-1.nip.io:8080`
  - `grafana.127-0-0-1.nip.io:8080`
  - `prometheus.127-0-0-1.nip.io:8080`
  - `alertmanager.127-0-0-1.nip.io:8080`
  - `argo.127-0-0-1.nip.io:8080` (Workflows UI)
  - `app.127-0-0-1.nip.io:8080` (custom app)
  - `webhook.127-0-0-1.nip.io:8080` (optional, for poking Argo Events from outside)
- **Each platform app gets an `Ingress` resource** with the right hostname. Helm values for each chart set the `ingress.enabled=true` block.

### TLS

Default to plain HTTP. It's a local demo. If you want green padlocks for screenshots, the upgrade path is:

1. Install cert-manager.
2. Use a mkcert-generated local CA, loaded as a `Secret` and exposed via a `ClusterIssuer` of type `ca`.
3. Annotate ingresses with `cert-manager.io/cluster-issuer: mkcert-ca`.
4. Add the mkcert CA to your system trust store once: zero browser warnings forever.

This is a clean optional layer, not part of the critical path.

### What does NOT need ingress

- gNMIc → Prometheus (in-cluster)
- Alertmanager → Argo Events webhook (in-cluster Service)
- gNMIc → SR Linux gNMI ports (in-cluster, ClusterIP)
- CNPG database (in-cluster, ClusterIP)
- Workflow → Slack (outbound from cluster, no ingress involved)

The Clabernetes vxlan link mesh stays entirely intra-cluster too.

---

## 4. Component inventory

Every piece, what it does, how it gets installed.

### Cluster-level (pre-ArgoCD)

| Component | Why | Install |
|---|---|---|
| k3d cluster | The host | `k3d cluster create -c k3d/config.yaml` |
| Traefik | Ingress (built-in) | comes with k3s |
| local-path provisioner | PVCs (built-in) | comes with k3s |
| metrics-server | HPA / `kubectl top` (built-in) | comes with k3s |
| ArgoCD | GitOps brain | helm install (bootstrap only) |
| Root `Application` | App-of-Apps entry point | `kubectl apply` |

### Platform (ArgoCD-managed, sync wave -1 / 0)

| Component | Purpose |
|---|---|
| cert-manager | Optional TLS, also required by some operators |
| sealed-secrets *or* SOPS+age | Slack webhook, any other secrets in git |
| CNPG operator | Postgres for the custom app |
| kube-prometheus-stack | Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics |
| **Loki (single-binary)** | **Log storage, datasource alongside Prom in Grafana** |
| **Grafana Alloy (DaemonSet)** | **Log shipping (k8s pod logs) + syslog receiver for SR Linux** |
| Argo Workflows | Workflow engine |
| Argo Events | EventSource + Sensor framework |
| Clabernetes operator | Runs containerlab topologies as pods |

### Workloads (ArgoCD-managed, sync wave 1+)

| Component | Purpose |
|---|---|
| Clabernetes `Topology` | The SR-MPLS network: 2 PE + 2 P SR Linux |
| gNMIc Deployment | Subscribes to interface state on every node, exposes Prom metrics |
| `PrometheusRule` | `oper-status != UP for 30s` → fires `InterfaceDown` |
| `AlertmanagerConfig` | Routes `InterfaceDown` to Argo Events webhook |
| Argo `EventSource` (webhook) | Receives Alertmanager POSTs |
| Argo `Sensor` | Filters, triggers `WorkflowTemplate` |
| Argo `WorkflowTemplate` | Parse alert → format → Slack post |
| **NetBox** | **Source of truth: devices, sites (with lat/lon), interfaces, cables, IPs. Replaces both the prior topology-service idea and the placeholder "custom app".** |
| **NetBox CNPG `Cluster`** | **Postgres for NetBox via CNPG operator** |
| **NetBox seed Job** | **One-shot Job that populates NetBox via REST API with the demo topology (idempotent)** |
| **NetBox Redis** | **Required by NetBox (Bitnami chart or bundled)** |
| Grafana `Dashboard` ConfigMap | Interface state, traffic, alert overlay |
| ~~Custom app: API + Frontend + DB~~ | ~~Folded into NetBox for v1. See §11 if you want a separate one.~~ |

### Secrets

| Secret | Where it lives | How |
|---|---|---|
| Slack webhook URL | git (encrypted) | sealed-secrets / SOPS |
| ArgoCD admin password | bootstrap script generates, stored locally | not in git |
| Grafana admin password | helm values, plaintext default for local | OK for demo |
| CNPG creds | generated by operator | k8s Secret, not in git |
| mkcert CA (if TLS) | local install only | not in git |

---

## 5. Resources budget

Working set with everything running, SR Linux ARM64 on Mac M-series:

| Component | RAM |
|---|---|
| 8× SR Linux nodes (2 TMC + 6 hubs) | ~12 GB |
| 4× FRR field-cabinet sims | ~800 MB |
| kube-prometheus-stack | ~2 GB |
| NetBox (app + redis + CNPG postgres) | ~1.5 GB |
| Loki + Alloy (DaemonSet) | ~700 MB |
| ArgoCD | ~500 MB |
| Argo Workflows + Events | ~300 MB |
| CNPG operator | ~200 MB |
| Clabernetes operator | ~200 MB |
| gNMIc (8 targets) | ~150 MB |
| k3s system + Traefik | ~1 GB |
| **Total working set** | **~20 GB** |
| **Recommended host RAM** | **32+ GB** (you have 64–96 GB, plenty of headroom) |
| **Disk** | ~50 GB images + state |
| **CPU** | 8 cores minimum, 12+ comfortable for build + demo runs |

Mac M-series is the sweet spot here — SR Linux multi-arch images run native, no Rosetta. Linux x86 also fine.

---

## 6. Repo layout

Single repo, clear separation between bootstrap (manual once), platform (ArgoCD owns), and workloads (ArgoCD owns).

```
demo/
├── README.md
├── Makefile                          # up, down, demo-cut, demo-restore
├── k3d/
│   └── config.yaml                   # cluster shape, port maps, registry
├── bootstrap/
│   ├── argocd-install.sh             # helm install argocd, wait, get pwd
│   └── root-app.yaml                 # the App-of-Apps Application
├── argocd/
│   ├── projects/                     # AppProject definitions
│   ├── platform/                     # one Application per platform component
│   │   ├── cert-manager.yaml
│   │   ├── sealed-secrets.yaml
│   │   ├── cnpg-operator.yaml
│   │   ├── kube-prometheus-stack.yaml
│   │   ├── loki.yaml
│   │   ├── alloy.yaml
│   │   ├── argo-workflows.yaml
│   │   ├── argo-events.yaml
│   │   └── clabernetes.yaml
│   └── workloads/                    # one Application per workload
│       ├── topology.yaml
│       ├── gnmic.yaml
│       ├── observability-config.yaml # rules, dashboards, AM config
│       ├── eventing.yaml             # eventsource + sensor + workflowtemplate
│       └── app.yaml
├── platform/                         # raw manifests / helm values for platform
│   └── values/
│       ├── kube-prometheus-stack.yaml
│       ├── loki.yaml
│       ├── alloy.yaml                # k8s log scrape + syslog receiver config
│       ├── argo-workflows.yaml
│       └── ...
├── workloads/
│   ├── topology/
│   │   ├── topology.yaml             # Clabernetes Topology CR
│   │   └── startup-configs/          # per-node SR Linux config
│   │       ├── pe1.cfg
│   │       ├── pe2.cfg
│   │       ├── p1.cfg
│   │       ├── p2.cfg
│   │       ├── p3.cfg
│   │       └── p4.cfg
│   ├── gnmic/
│   │   ├── deployment.yaml
│   │   ├── configmap.yaml            # gnmic config: targets, subscriptions, prom output
│   │   └── service.yaml              # ClusterIP for Prom scrape
│   ├── observability/
│   │   ├── prometheusrules.yaml      # alert rules + link_status recording rule
│   │   ├── alertmanagerconfig.yaml
│   │   ├── link-membership-info.yaml # static link_membership_info{node, interface, link_id}
│   │   └── dashboards/
│   │       ├── network-overview.json # Geomap + NodeGraph
│   │       ├── links.geojson         # static link geometry (rendered from spec)
│   │       ├── node-detail.json
│   │       ├── link-detail.json
│   │       ├── alert-console.json
│   │       └── workflow-activity.json
│   ├── eventing/
│   │   ├── eventsource-webhook.yaml      # alert + manual-cut webhooks
│   │   ├── sensor-interface-down.yaml    # routes alerts to enriched-notify WFT
│   │   ├── sensor-manual-cut.yaml        # routes manual-cut button to cut-fiber WFT
│   │   ├── wft-enriched-notify.yaml      # 3-4 step DAG: enrich → analyze → notify → journal
│   │   ├── wft-cut-fiber.yaml            # WorkflowTemplate that does the gNMI set
│   │   └── scripts/
│   │       ├── enrich.py                 # NetBox query logic
│   │       ├── analyze_impact.py         # graph-walk + impact reasoning
│   │       └── slack_blocks.py           # Block Kit message builder
│   └── netbox/
│       ├── chart-values.yaml          # netbox-community/netbox-chart values
│       ├── cnpg-cluster.yaml          # CNPG Postgres for NetBox
│       └── seed-job.yaml              # idempotent seed of devices/sites/cables
├── spec/
│   └── atlanta.yaml                   # canonical topology spec — drives startup configs, gNMIc tags, NetBox seed
├── tools/
│   └── render/                        # Go template renderer: spec → SR Linux configs + gNMIc targets + NetBox seed payload
└── docs/
    ├── architecture.md
    ├── runbook-demo.md
    └── runbook-troubleshoot.md
```

### Sync waves

- `-1` cert-manager, sealed-secrets
- `0` operators: CNPG, Argo Workflows, Argo Events, Clabernetes, kube-prometheus-stack, Loki, Alloy
- `1` NetBox (chart + CNPG cluster), Clabernetes Topology, gNMIc, observability config (rules/dashboards/AM)
- `2` NetBox seed Job (depends on NetBox ready), eventing (eventsource/sensor/workflowtemplate)

---

## 7. Demo data path — concrete wiring

### Subscription
gNMIc config subscribes to each SR Linux target on `/interfaces/interface/state` with `STREAM SAMPLE 10s`. Targets and per-target tags (`role`, `site`, `lat`, `lon`) are rendered from the `atlanta.yaml` spec at deploy time. Those tags become Prometheus labels on every metric — Grafana's Geomap pulls per-marker lat/lon directly from them, no join needed.

For link-level visualization, a Prometheus recording rule rolls up per-interface oper-status to per-link status using a static `link_membership_info{node, interface, link_id}` series (also rendered from the spec, exposed via a tiny configmap-backed static-info exporter, or via Prometheus relabel rules on the gNMIc scrape — implementation detail, several clean options). The rule:

```
- record: link_status
  expr: min by (link_id) (
    oper_status_value * on(node, interface) group_left(link_id) link_membership_info
  )
```

Grafana's GeoJSON layer then matches `link_id` from each feature against `link_id` from this rule's output to color links green/red.

### Metric exposition
gNMIc `prometheus` output plugin exposes `:9804/metrics`. A Service + ServiceMonitor (kube-prometheus-stack picks it up).

### Alert rule
```
oper_status_value{interface=~"ethernet-1/.*"} != 1
```
Hold 30s, label `severity=critical, alertname=InterfaceDown`.

### Alertmanager route
`AlertmanagerConfig` matches `alertname=InterfaceDown`, sends to webhook receiver:
```
http://eventsource-webhook.argo-events.svc:12000/fiber-cut
```

### Argo Events
- `EventSource` kind=`webhook`, listens `:12000`, path `/fiber-cut`.
- `Sensor` filters `body.alerts[0].labels.alertname == "InterfaceDown"`, triggers `WorkflowTemplate` `slack-notify` with the alert payload as a parameter.

### Workflow — the enrichment story (DOT spin + DOM diagnostics)

The whole demo narrative shifts here. Anyone can fire a webhook to Slack. The valuable thing is what comes after: pulling rich context from a source of truth, querying live optical telemetry, and turning a raw alert into a fiber-crew dispatch order.

**WorkflowTemplate:** `enriched-link-down-notify`

**Inputs** (extracted by the Sensor from the Alertmanager payload): `node`, `interface`, `alertname`, `severity`, `fingerprint`, `started_at`.

**DAG (4 steps + optional 5th, all visible in the Argo Workflows UI — the visual graph is part of the demo):**

**Step 1 — `enrich`** (`python:3.12-slim` + `pynetbox`)
Queries NetBox for the affected interface and walks outward:
- Device + site (lat/lon, GDOT district, on-call rotation, maintenance contract)
- Interface description, IP/VRF assignment, parent fiber pair
- Cable record: cable ID, label, route description (e.g., *"I-75 northbound, MM 257-262"*), length in km, install date, FOC vendor
- **Far-end device + interface, derived from the cable record** — implicit from source of truth, not from a config file
- Fiber circuit: provider, circuit ID, contract reference, restoration SLA
- Tenants/agencies riding the link: Cobb County DOT, City of Marietta, MARTA, etc.
- ITS service inventory behind this path: count of CCTV cameras, signal controllers, DMS, ramp meters reachable

Output: a single JSON artifact passed to the next step.

**Step 2 — `collect-dom`** (gNMIc image, runs in parallel arms — both endpoints simultaneously)
Queries the transceivers on both ends of the affected link via gNMI:
- `/interfaces/interface[name=X]/transceiver/state/input-power` (Rx dBm)
- `/interfaces/interface[name=X]/transceiver/state/output-power` (Tx dBm)
- `/interfaces/interface[name=X]/transceiver/state/module-temperature`
- `/interfaces/interface[name=X]/transceiver/state/laser-bias-current`

Both endpoints come from the cable record in step 1 — the workflow doesn't need to be told which neighbor to ask, NetBox already said.

**Honest note on faking:** containerized SR Linux in Clabernetes doesn't have real SFPs, so `transceiver/state` may report zeros or be absent. The workflow tries the real query first; if values are absent or implausible, it synthesizes plausible numbers based on the interface's operational state (admin-disabled → Rx ≈ -40 dBm "no light"; oper-up → Rx ≈ -3 to -7 dBm typical SR optic). The demo is about the *workflow pattern* — the simulation is at the data-source level only and is documented in `docs/architecture.md`.

**Step 3 — `analyze`** (`python:3.12-slim`, no extra deps)
Combines NetBox + DOM data into actionable diagnosis:

*Optical diagnosis* (compare Rx/Tx on both ends):
- Both Rx low + both Tx normal → **bidirectional fiber cut** (most common; both ends transmitting, neither hearing back)
- One Rx low, other Rx normal → **single-strand failure** (unidirectional cut or splice issue)
- Both Rx normal but oper-down → **higher-layer fault** (light fine, but link not up — possible LACP, MTU, or admin-state issue)
- Both Rx critically high → **missing attenuator** (over-saturation, often after a maintenance event)

*Topology impact* (graph walk over NetBox cables):
- Are there alternate physical paths? Is TI-LFA active?
- Capacity reduction estimate
- Single-homing exposures (which downstream devices have no alternate path)

*Service impact*:
- Per-agency rollup (which tenants are affected, how)
- Per-service-class rollup (CCTV count, signal count, DMS count)
- Restoration ETA based on circuit contract SLA

**Step 4 — `notify`** (`python:3.12-slim` + `requests`)
Builds the Slack Block Kit message. Sample:

> 🚨 **ITS Network Critical: Fiber Down — `tmc-1` ↔ `hub-nw`**
>
> **Cable:** `FOC-NW-01` *I-75 corridor northbound, MM 257-262*, 4.8 km, installed 2022-09-14
> **Provider:** Crown Castle Fiber, circuit `CCFL-12345`, restoration SLA 4hr
>
> **Optical diagnostics:**
> - `tmc-1 ethernet-1/1` — Tx **+1.2 dBm** ✅ &nbsp;|&nbsp; Rx **-40 dBm** ❌
> - `hub-nw ethernet-1/4` — Tx **+0.8 dBm** ✅ &nbsp;|&nbsp; Rx **-40 dBm** ❌
> - **Diagnosis: bidirectional fiber cut.** Both ends transmitting normally, neither receiving. Optics OK.
>
> **Operational impact:**
> - Affected agencies: Cobb County DOT, City of Marietta
> - ITS devices behind this path: 24 CCTV, 87 signal controllers, 4 DMS
> - Alternate path active via `hub-n` (TI-LFA <50ms reroute)
> - Corridor capacity reduced ~33%
>
> **Recommended action:** dispatch fiber crew to inspect cable along I-75 NB between MM 257-262. Likely splice failure or excavation damage.
>
> [📍 NetBox cable] &nbsp;[📊 Grafana] &nbsp;[📖 Runbook] &nbsp;[📞 Page on-call] &nbsp;[🔕 Ack]
>
> _Fired 14:23:07 EDT • alertmanager `a3f9...`_

**Optional Step 5 — `journal-netbox`**
POST a NetBox journal entry on both endpoint devices *and* the cable record. Auto-generated change log: future-you searching NetBox for "what happened on FOC-NW-01" gets a timeline. Real DOT operations practice.

### Why this changes the demo narrative

For DOT/ITS audiences, the headline shifts from:

> *"We can route alerts to Slack."*

to:

> *"Source of truth + live optical telemetry produces a fiber-crew dispatch order with mile markers, agency-level impact, and SLA-aware restoration ETA. From alert to action in seconds, no human triage."*

Anyone in a TMC will recognize that as the operational model they want and rarely have. It's the explicit value prop for the engagement.

This also means the **NetBox seed has to be DOT-rich** — agency tenants (City of Atlanta, Fulton/Cobb/DeKalb DOT, MARTA, GSP), corridor cables with route descriptions and mile markers, fiber providers (Crown Castle, Lumen managed services, GDOT-owned fiber), GDOT-relevant custom fields (district, on-call NMS engineer, last-touched), seeded journal entries showing prior cable events. ~150 lines of seed JSON. Covered in §11 (Open decisions).

---

## 8. Observability portfolio — dashboards and alerts

The demo lives or dies on this layer. Concrete plan.

### Dashboards

All provisioned via ConfigMap + Grafana sidecar discovery. Nine dashboards organized in three folders: **Network**, **Operations**, **Platform**.

**Network folder** — the routing/topology view, what TMC operators care about.

**1. Atlanta Network Overview** — landing page, Atlanta-centric.
- **Hero panel: Geomap** centered on Atlanta metro. Two layers:
  - *Marker layer (nodes)* — six device points, lat/lon coming straight from Prometheus label values (gNMIc tags those onto every metric per target). Color-thresholded on rolled-up oper-status (green = all interfaces up, amber = degraded, red = node unreachable). Hover shows node name, role, active alerts.
  - *GeoJSON layer (links)* — LineString features for each physical link. Geometry is static (rendered from `spec/atlanta.yaml` at build time into `links.geojson`, committed alongside the dashboard ConfigMap). Each feature carries a `link_id` property (e.g., `tmc1-hubnw`, `hubn-hube`).
    - Coloring is dynamic via Grafana Geomap's data-bound style rules: a Prometheus query (`link_status == 0`) returns the set of currently-down links by `link_id`, the panel matches `link_id` against feature properties, red is applied where matched, green elsewhere.
    - Animated dashed stroke on red links — visually signals "active outage" in screen recordings.
  - Base map: CARTO dark or OSM dark.
- Companion panel: NodeGraph for the routing/protocol view (IS-IS adjacencies, BGP sessions). Different lens on the same data — the geo map shows the physical/geographic story, NodeGraph shows the logical/protocol story.
- Stat panels: total interfaces, up/down counts, active alerts, BGP/ISIS adjacency totals.
- Recent alert annotations overlaid on a small traffic time-series.
- Default 1h range, 5s refresh during demo, 30s normally.

**2. Network Health** — operational rollup, "is everything OK right now?"
- Top stats row: nodes up/down, interfaces up/down, ISIS adjacencies (current vs expected), BGP sessions (current vs expected). Reds appear immediately when something is missing.
- Convergence events (last 1h): link transitions, ISIS LSP churn, BGP UPDATE rate.
- Aggregate corridor traffic: TMC ingress/egress totals, broken out by corridor.
- Top N talkers (interfaces by bps, last 5 min).
- SLA per link: % uptime over last 24h, ranked worst-first.
- Alert burn-down: critical/warning counts over time.
- This is the dashboard that goes on a TMC NOC wall.

**3. ITS Service Health** — DOT-flavored, per-agency views.
- Per-agency rollup table: agency name, paths traversed, capacity, latency, packet loss, alert count. (Joined via Prom recording rules using NetBox tenant tags.)
- Service-class breakdown: CCTV bytes/sec, signal control rate, DMS update count, ramp-meter heartbeats.
- Tenant-impacting alerts feed.
- Dashboard the enriched Slack alert links to ("see all impacts on Cobb DOT") — this is where the click lands.

**4. Node Detail** — drill-down per SR Linux node, organized as tabs.
- Variable for node selection.
- *Interfaces* tab: per-interface oper-status, traffic in/out, error/discard counters.
- *Routing* tab: ISIS adjacencies with neighbor IPs, BGP session state, prefix-SIDs received, LFIB summary, adjacency-SIDs.
- *Device Health* tab: CPU per process (control vs data plane), memory utilization, hardware temperatures, fan speed, PSU status, recent process restarts, last reboot timestamp, recent config commits.
- *Optical* tab: DOM stats per interface, Rx/Tx power sparklines, deviation from baseline.
- *Logs* tab: live tail of node logs from Loki, filtered by `node` label — pivot from a metric anomaly to the actual log line that caused it.

**5. Link Detail** — drill-down per interface.
- Throughput, errors, drops, queue depth, sub-interface breakdown.
- Optical panel: Rx/Tx dBm time series, threshold lines, deviation from baseline.
- Cable info panel: NetBox-fed (label, route description, mile markers, length, install date, FOC vendor).
- Logs panel: Loki query for log lines mentioning this interface across all nodes, time-windowed.

**6. Routing & SR Detail** — SR-specific operational view.
- Per-prefix-SID utilization (which routers are forwarding labeled packets where).
- TI-LFA backup path coverage (which links have precomputed FRR alternates).
- IS-IS LSDB size + churn rate.
- BGP table size + UPDATE rate.
- ECMP path count distribution per destination.

**Operations folder** — the workflow + alerting view.

**7. Alert Console** — what's firing, with log overlay.
- Active alerts grouped by severity, recent firings (24h timeline), MTTR for resolved alerts.
- Each row deep-links to Network Overview with the affected link highlighted.
- Each alert annotation includes a runbook URL.
- Inline Loki panel: log lines from the affected node around alert fire time.

**8. Workflow Activity** — Argo Workflows runs.
- Recent runs by template, success/failure ratio, average duration.
- Filter to see `enriched-link-down-notify` runs specifically — directly demonstrates the closed loop.
- Each row deep-links to the workflow's Loki log stream — see the enrich step's NetBox query, the collect-dom step's gNMIc output, the analyze step's reasoning, all inline.

**Platform folder** — cluster + supporting services health.

**9. Platform Health** — boring-but-essential.
- Pod restarts, CNPG cluster health, NetBox API latency, Loki ingestion rate, gNMIc subscription lag, Argo Events trigger rate, Prometheus rule evaluation latency.

### Logs alongside metrics — Loki

Whole observability portfolio binds together when logs sit next to metrics in the same Grafana UI. Without Loki, the demo shows "metrics fired an alert"; with Loki, it shows "metrics fired an alert *and here's the log line from the SR Linux node showing the interface go down 200ms before Prometheus saw it.*" That's a real operational pattern.

**Stack:**
- **Loki** (single-binary mode, ~500 MB) — log storage, indexed by labels.
- **Grafana Alloy** (the unified telemetry agent that supersedes Promtail, ~200 MB, deployed as DaemonSet) — scrapes Kubernetes pod logs and listens for syslog from SR Linux on a dedicated port.
- **Loki datasource** in Grafana, exposed alongside Prometheus.
- **SR Linux syslog forwarder** — each node configured (via startup-config, rendered from spec) to send syslog to Alloy's syslog receiver.

**What this enables in the demo:**
- *Pivot from alert to logs*: alert fires → click affected node → Node Detail dashboard → Logs tab shows SR Linux process logs around the cut, including the kernel-level interface state-change event.
- *Pivot from workflow to logs*: Workflow Activity → click a run → Loki shows the Python step's stdout: the actual NetBox query, the DOM values returned, the impact analysis output. Demystifies the workflow without needing to dig in Argo's UI.
- *Pivot from interface to logs*: Link Detail → Logs panel shows everything mentioning that interface across every node in the time window.
- *Audit trail*: ArgoCD sync events, NetBox journal entries, Alertmanager fires/resolves — all searchable in Loki.

**Resource cost:** ~700 MB total. One Deployment (Loki) + one DaemonSet (Alloy).

### Visual polish

- Dark theme default.
- Consistent palette: muted blue/green for normal, amber for warning, red for critical. No rainbow defaults.
- Custom Grafana org name + logo (placeholder you replace).
- Refresh rate as a dashboard variable so it can be 5s on demo day, 30s otherwise.
- Every panel gets a tooltip explaining its metric.
- Alertmanager fire/resolve annotations overlay every time-series panel — visually obvious when a fire happens.

### Interactive demo affordance (nice-to-have)

A Grafana button panel on the Network Overview: "Trigger fiber cut: P3↔P1". POSTs to a tiny in-cluster service that exec's into the SR Linux pod. Recovery button next to it. Makes the demo a click instead of `make demo-cut`. Pure polish, v1.1.

### Alert taxonomy

Tiered, so a single fiber cut produces one meaningful alert, not fifty.

| Tier | Examples | Routing |
|---|---|---|
| **Critical** | Interface down >30s, BGP session down >60s, node unreachable, IS-IS adjacency lost | Slack `#netops-critical` + Argo Events workflow |
| **Warning** | Interface error rate elevated, BGP flap detected, IS-IS adjacency unstable, oper-status flapping | Slack `#netops-warnings` |
| **Info** | Config change detected, planned-maintenance window entered, gNMIc lag elevated | Slack `#netops-info` (or dashboard-only) |

**Inhibition rules** — when `NodeUnreachable` fires for a given node, suppress all per-interface alerts for that node. One alert with full context, not spam.

**Grouping** — Alertmanager `group_by: [cluster, alertname, severity]`, `group_wait: 30s`, `group_interval: 5m`. A multi-link cut is one notification, not several.

**Rich annotations** — every alert template includes:
- `summary`: one-line human description with `{{ $labels.interface }}` / `{{ $labels.node }}` interpolated.
- `description`: paragraph with metric context, last-known-good timestamp, and impact statement.
- `runbook_url`: link into the same repo's `docs/runbooks/`.
- `dashboard_url`: deep link to the relevant Grafana dashboard with vars pre-filled.

### Source dashboards to start from

- gNMIc repo ships example Grafana dashboards for SR Linux — fork those as a baseline rather than starting blank.
- Nokia maintains community SR Linux dashboards; cherry-pick panels.
- kube-prometheus-stack already provisions cluster/node/pod dashboards — we keep those as-is for the platform health view.

---

## 9. Failure injection

### Primary: gNMIc set, two paths

Both paths converge on the same operation: SSH/CLI exec into the SR Linux pod and disable the interface. Two ways to invoke:

**Path A — Makefile (CLI):**
```
make demo-cut INTERFACE=ethernet-1/1 NODE=tmc-1
```
Wraps:
```
kubectl exec -n clabernetes <tmc-1-pod> -- \
  sr_cli "enter candidate; \
          set interface ethernet-1/1 admin-state disable; \
          commit now"
```

**Path B — Grafana button → Argo Events:**
Grafana button panel POSTs to an Argo Events webhook EventSource (`/manual-cut`). A Sensor matches and triggers a `cut-fiber` WorkflowTemplate, which performs the same `kubectl exec` (or gNMIc set, equivalently) inside a workflow pod with appropriate RBAC.

This dogfoods the platform: the same Argo Events + Workflow chain that processes Alertmanager alerts also processes manual demo triggers. One eventing surface, two upstream callers.

### Recovery
```
make demo-restore INTERFACE=ethernet-1/1 NODE=tmc-1
```
Or the Grafana "Restore" button next to "Cut" — POSTs to a `/manual-restore` EventSource.

### Alternative trigger
`kubectl scale deployment` on a Clabernetes-managed pod to 0. Cruder, simulates total node loss. Useful for showing different alert classes.

---

## 10. Topology design — Atlanta DOT / ITS network (District-rich)

12 nodes total: 2 TMC + 6 corridor hubs (8 SR Linux) + 4 FRR field-cabinet simulators. Modeled on a real GDOT District 7 corridor network with the I-285 perimeter ring as the spine.

```
                                    fc-n
                                     │
                                     │ (GA-400 N spur)
                  ┌─────────────────hub-n─────────────────┐
                  │                                       │
                  │  ┌──────────────tmc-2────────────────┐│
                  │  │                                   ││
              ┌───┴──┴┐                               ┌──┴┴───┐
   fc-nw ── hub-nw    │   I-285 perimeter ring        │  hub-e
  (I-75 NW)    │      └───────────┐         ┌─────────┘   │
               │                  │         │             │
               │                  │         │           hub-i20e ── fc-i20e
               │                  │         │             │       (I-20 E)
               │                  │         │             │
            tmc-1                 │         │             │
        (Forest Pk)               │         │             │
               │                  │         │             │
               │                  │         │             │
   fc-sw ── hub-sw                │         │           hub-i20w
  (I-85 SW)    │                  │         │            (I-20 W)
               │                  │         │             │
               └──────────────────┴─────────┴─────────────┘
                              I-285 ring continues
```

(That ASCII compromise gets the gist — for the real picture, see the Geomap dashboard.)

### Backbone shape

**8 SR Linux nodes:**
- **tmc-1** (Forest Park) — primary TMC head end
- **tmc-2** (Buckhead) — backup TMC, active-active via iBGP
- **hub-n** (Sandy Springs, GA-400 corridor)
- **hub-e** (Decatur, I-285 east)
- **hub-i20e** (Lithonia, I-20 east corridor)
- **hub-nw** (Marietta, I-75 NW corridor)
- **hub-sw** (East Point, I-85 SW corridor)
- **hub-i20w** (Lithia Springs, I-20 west corridor)

**4 FRR field-cabinet simulators** (CE-style, attached to one hub each via /30):
- **fc-n** (Alpharetta, on the GA-400 corridor) → hub-n
- **fc-nw** (Kennesaw, on the I-75 NW corridor) → hub-nw
- **fc-i20e** (Conyers, on the I-20 east corridor) → hub-i20e
- **fc-sw** (Newnan, on the I-85 SW corridor) → hub-sw

Cabinets don't run streaming telemetry (FRR doesn't have it), don't need it, and don't run SR. Each has a loopback, a /30 to its parent hub, a default route upstream, and an eBGP session announcing its loopback prefix into the GDOT backbone. They exist to:
- Be visible in NetBox as "Field Cabinet" devices with agency tenants and ITS service inventory custom fields ("24 CCTV cameras, 18 signal controllers, 3 DMS")
- Get isolated when their uplink is cut, surfacing realistic agency/device counts in the enriched alert
- Demonstrate end-to-end paths in dashboards, not just backbone-to-backbone

### Links (15 total)

- **I-285 perimeter ring (6 links):** hub-n ↔ hub-e ↔ hub-i20e ↔ hub-sw ↔ hub-i20w ↔ hub-nw ↔ hub-n
- **TMC redundant uplinks (4 links):** tmc-1 ↔ hub-nw, tmc-1 ↔ hub-sw, tmc-2 ↔ hub-n, tmc-2 ↔ hub-e
- **TMC primary-backup direct (1 link):** tmc-1 ↔ tmc-2
- **Cabinet uplinks (4 links):** fc-n ↔ hub-n, fc-nw ↔ hub-nw, fc-i20e ↔ hub-i20e, fc-sw ↔ hub-sw

### Routing

- IS-IS L2 single area on all 8 SR Linux nodes
- SR-MPLS globally enabled across the SR Linux backbone
- Prefix-SIDs:
  - tmc-1=16001, tmc-2=16002
  - hub-n=16101, hub-e=16102, hub-i20e=16103, hub-nw=16104, hub-sw=16105, hub-i20w=16106
- TI-LFA on all SR Linux interfaces (sub-50ms convergence on link/node loss)
- iBGP tmc-1 ↔ tmc-2 (with hub-n acting as RR for v1.x if you want to demonstrate RRs)
- eBGP between each FRR cabinet and its parent hub (separate small AS per cabinet — a v2 polish would be putting all cabinets in one "agency AS" with iBGP among them, but it doesn't add demo value)

### Failure scenarios this topology demonstrates

- **Corridor fiber cut (TMC↔hub):** primary use case for the DOM-enriched workflow. Headline cut: tmc-1 ↔ hub-nw (I-75 NW corridor).
- **Ring segment cut (hub↔hub):** TI-LFA absorbs, alternate ring path takes over. Demonstrates fast reroute clearly.
- **Hub isolation (both uplinks down):** corridor hub becomes unreachable, all field cabinets behind it lose central control.
- **Cabinet uplink cut (hub↔fc):** specific corridor's ITS edge isolated. Surfaces in alert as "fc-nw isolated, 24 CCTV + 18 signal controllers + 3 DMS unreachable, agency: City of Marietta."
- **TMC failure (primary):** tmc-2 takes over via iBGP, traffic re-steers, demonstrates active-active TMC pattern.

The headline demo cut stays **tmc-1 ↔ hub-nw** — visually clean on the geomap (line drops running NW from Forest Park toward Marietta), optical diagnosis is unambiguous, agency rollup names Cobb County DOT specifically.

### Geographic placement (Atlanta metro, all 12 nodes)

Real Atlanta-area lat/lon. Picked to roughly match where actual GDOT facilities, corridor hubs, and field cabinets would sit.

| Node | Role | NetBox Site | Lat | Lon |
|---|---|---|---|---|
| tmc-1 | TMC primary | Forest Park (near GDOT TMC) | 33.6195 | -84.3705 |
| tmc-2 | TMC backup | Buckhead Regional Ops | 33.8484 | -84.3781 |
| hub-n | Corridor hub (GA-400) | Sandy Springs | 33.9304 | -84.3733 |
| hub-e | Corridor hub (I-285 east) | Decatur | 33.7748 | -84.2963 |
| hub-i20e | Corridor hub (I-20 east) | Lithonia | 33.7126 | -84.1110 |
| hub-nw | Corridor hub (I-75 NW) | Marietta | 33.9526 | -84.5499 |
| hub-sw | Corridor hub (I-85 SW) | East Point | 33.6795 | -84.4394 |
| hub-i20w | Corridor hub (I-20 west) | Lithia Springs | 33.7793 | -84.6427 |
| fc-n | Field cabinet (GA-400) | Alpharetta | 34.0754 | -84.2941 |
| fc-nw | Field cabinet (I-75 NW) | Kennesaw | 34.0234 | -84.6155 |
| fc-i20e | Field cabinet (I-20 E) | Conyers | 33.6679 | -84.0177 |
| fc-sw | Field cabinet (I-85 SW) | Newnan | 33.3807 | -84.7997 |

The 12-node placement gives the geomap a wider, more recognizable Atlanta-metro footprint than the 6-node version — TMCs in the inner core, hubs at the I-285 perimeter, cabinets out at the metro edges.

These coords live in `spec/atlanta.yaml` as the canonical input. From that single spec, four things get generated at deploy time:

1. **Per-node startup configs** — SR Linux configs for backbone (loopbacks, IS-IS, SR-MPLS, BGP, interface IPs) plus FRR configs for cabinets (loopback, /30, default route, eBGP) → committed alongside or rendered by an init job before Clabernetes deploys
2. **gNMIc target list with tags** (`lat`, `lon`, `role`, `site`, `district`) for the 8 SR Linux backbone nodes — cabinets aren't subscribed to (FRR has no gNMI). Tags become Prometheus labels.
3. **NetBox seed payload** — devices, sites with lat/lon, GDOT-flavored custom fields, interfaces, cables (with corridor route descriptions and mile markers), IP addresses, agency tenants, fiber circuits, ITS service inventory per cabinet, seeded journal entries → posted to NetBox API by the seed Job
4. **Static `links.geojson`** — LineString features for each of the 15 physical links, each with a `link_id` and `corridor` property → committed alongside the dashboard ConfigMap, loaded by the Grafana Geomap GeoJSON layer

NetBox serves as the runtime browse/API source of truth for the enrichment workflow. Grafana queries Prometheus directly for status (lat/lon already on every metric); enrichment workflow hits NetBox API for cable routes, agency rollups, ITS device counts behind affected cabinets, and circuit SLA data.

### Scaling beyond District-rich

District-rich is the v1 target (12 nodes, ~20 GB). Two reasonable points to scale up to from there:

| Size | Nodes | Working RAM | What it adds, in DOT terms |
|---|---|---|---|
| **District-rich (v1 target)** | 2 TMC + 6 corridor hubs + 4 FRR cabinets | ~20 GB | Full GDOT District 7 corridor coverage, end-to-end TMC-to-edge paths, agency-aware enrichment with real device counts |
| **District-rich+** | + 2 more cabinets, + dual-homed cabinet, + L3VPN VRF per agency | ~22 GB | Per-agency VRF isolation demonstrable, dual-homed cabinet shows ECMP at the edge, more interesting failure matrix |
| **Statewide** | Atlanta district (full District-rich) + Macon/Savannah district (1 TMC + 3 hubs + 2 cabinets) + 2 statewide-backbone routers | ~30 GB | Inter-district SR-MPLS, statewide GDOT operations, multi-TMC failover across districts. Dashboards group by district. Closest to actual GDOT scale. |

Because `tools/render/` reads `spec/atlanta.yaml` (or `spec/georgia.yaml` for the statewide variant), scaling 12 → 14 → 18+ is a spec edit, not a rewrite. The render pipeline produces SR Linux configs, FRR configs, gNMIc targets, NetBox seed payload, and `links.geojson` from a single source — they all stay consistent.

**Recommendation for v1.x progression:**
- v1.0 — District-rich, Atlanta only, get the demo loop solid
- v1.1 — Add CE simulator routing realism (BGP with route policies, L3VPN per agency)
- v1.2 — Statewide expansion (great pitch material for full-GDOT engagements)

---

## 11. Open decisions

Things to nail down before we generate manifests:

1. **TLS now or later?** Recommend later. Plain HTTP gets us to a working demo in half the time.
2. **Secrets tool — sealed-secrets vs SOPS?** sealed-secrets is simpler per-cluster; SOPS is portable. For a single demo cluster, sealed-secrets wins.
3. **Separate custom app or NetBox-only?** NetBox folds in the GitOps + CNPG + frontend story. Recommend NetBox-only for v1. If you want a separate Go service later (e.g., a "scenario controller" that runs scripted multi-failure demos), it's a clean v1.x add.
4. **Slack vs email vs both?** Slack alone for v1. Email adds SMTP config complexity that doesn't add demo value.
5. **CE simulators (FRR) included?** Recommend yes for v1.1, not v1. Get the SR core working first.
6. **L3VPN demo or just plain IP?** Recommend plain IP for v1, L3VPN as v1.2.
7. **mkcert local CA?** Optional polish, ignore until everything else works.
8. **NetBox seed richness — DOT flavor.** The enriched-alert workflow (§7) only earns its keep if NetBox has *real* DOT operational data to surface. Recommend the seed Job populate: GDOT-flavored devices and sites (with district + on-call custom fields), corridor cables with route descriptions and mile markers (e.g. *"I-75 NB MM 257-262, 4.8km"*), fiber providers (Crown Castle, Lumen managed, GDOT-owned), agency tenants (City of Atlanta, Fulton/Cobb/DeKalb DOT, MARTA, GSP), VRFs per agency, IP prefixes per VRF, ITS service inventory (counts of cameras/signals/DMS/ramp meters per hub), and a few seeded journal entries showing prior cable events. ~150 lines of seed JSON. Without this, the impact-analysis step has nothing interesting to say.
9. **DOM faking strategy.** Containerlab's SR Linux doesn't have real SFPs — `transceiver/state` may report zeros or be absent. Three options: (a) workflow tries real query, falls back to synthesized values keyed off operational state — *recommended for v1, simplest, pattern stays real*; (b) pre-seed plausible static DOM values via gNMIc set at topology bring-up — adds one-time complexity; (c) couple DOM injection to the cut-fiber workflow itself (when interface is disabled, also "set" Rx values) — cleanest if SR Linux supports writing transceiver state, which it likely doesn't. Go with (a); document the simulation honestly in `docs/architecture.md`.
10. **gNMIc target source — rendered from spec, or pulled from NetBox API at start?** Render-from-spec is simpler and avoids a startup ordering dance. NetBox-pull is more "live" but earns its keep only when topology changes at runtime, which it doesn't for this demo.

---

## 12. Bring-up order

The one-time path from empty laptop to working demo:

1. Install prereqs: Docker (or OrbStack), `k3d`, `kubectl`, `helm`, `make`, optionally `mkcert`.
2. Clone repo.
3. `make up` →
   - `k3d cluster create -c k3d/config.yaml`
   - `bootstrap/argocd-install.sh` (helm install, wait for ready, print URL + admin password)
   - `kubectl apply -f bootstrap/root-app.yaml`
4. Watch ArgoCD UI at `argocd.127-0-0-1.nip.io:8080`. Sync waves do their thing. ~5–10 minutes for everything to land.
5. Verify:
   - SR Linux nodes converge IS-IS (`kubectl exec`, `show isis adjacency` on `tmc-1`, `hub-nw`, etc.)
   - gNMIc has all targets up, metrics include `lat`/`lon`/`role`/`site`/`district` labels
   - NetBox UI accessible, seed Job completed, devices visible at `netbox.127-0-0-1.nip.io:8080` with agency tenants and corridor cables populated
   - Grafana Geomap shows six green dots over Atlanta (tmc-1 at Forest Park, tmc-2 at Buckhead, four hubs spread)
   - Alertmanager receivers configured (`alertmanager.127-0-0-1.nip.io:8080/#/status`)
6. `make demo-cut INTERFACE=ethernet-1/1 NODE=tmc-1` (cuts the I-75 corridor link to hub-nw)
7. Watch the alert fire, the workflow run (4 steps visible in Argo UI), the Slack message arrive with optical diagnosis and corridor mile markers.
8. `make demo-restore INTERFACE=ethernet-1/1 NODE=tmc-1` to reset.
9. `make down` tears the cluster down completely.

---

## 13. What this gets you

- A complete, reproducible, self-hosted **DOT/ITS network operations reference architecture**: streaming telemetry → enriched alerts → optical diagnosis → fiber-crew dispatch order, with NetBox as the source-of-truth spine.
- A clean GitOps story (App-of-Apps, sync waves, declarative everything).
- A platform you can extend: swap NaviGAtor data feeds in, add Workflows that auto-create maintenance windows in NetBox, plug in real GDOT inventory, add SR-TE policies that re-steer traffic during scheduled corridor maintenance, etc.
- A portable demo for DOT/ITS engagements — laptop only, no cloud cost, runs on a plane.
- A storyboard that distinguishes the engagement from "we can write Ansible playbooks": you're showing operational maturity (source of truth + live optical telemetry → actionable dispatch), not just configuration management.

---

## 14. Out of scope (for v1)

- Multi-cluster / hub-spoke ArgoCD
- HA anything
- Real authentication on the UIs (uses default admin creds)
- Real RBAC on the workflow side
- Persistence beyond the demo session for Prom/Grafana data (we keep it but lifecycle is informal)
- Production-grade Clabernetes networking (we use the default vxlan mesh)
- IPv6 / SRv6 (stick to SR-MPLS for v1; SRv6 is a good v2 stretch)
- L3VPN per-agency VRFs (v1.1)
- Statewide multi-district expansion (v1.2)
- Real ITS protocol emulation on the FRR cabinets (NTCIP, RTSP, etc. — they're routing-only stubs in v1)

---

## 15. Building this with Claude Code

This file is the canonical plan. Drop it into a fresh repo and use the following kickoff prompt with Claude Code.

### Repo setup

```bash
mkdir gdot-demo && cd gdot-demo
git init
mkdir -p docs
# Save this PLAN.md into docs/PLAN.md
git add docs/PLAN.md
git commit -m "docs: import demo stack plan"

# Then:
claude  # start Claude Code in this directory
```

### Kickoff prompt for Claude Code

Paste this into Claude Code as the first message:

```
I want to build the demo stack described in docs/PLAN.md. Read the entire file
carefully — it's the complete architecture and the source of truth for this
project. We're targeting the District-rich tier (12 nodes total: 2 TMC + 6
corridor hubs as SR Linux, plus 4 FRR field-cabinet simulators). Working set
~20 GB.

Important constraints:
- Single laptop (Mac M-series or Linux x86), Docker (or OrbStack on Mac)
- All container images must be ARM64-compatible (I'm on Apple Silicon often).
  SR Linux ghcr.io/nokia/srlinux is multi-arch — use that.
- Free/OSS only. No commercial NOSes, no paid tools.
- GitOps-first: ArgoCD App-of-Apps, sync waves, declarative everything.
- My GitHub username is jp2195. Use that for any URLs/labels where applicable.

Build it incrementally in this order:

1. Bootstrap layer
   - k3d/config.yaml (1 server + 2 agents, registry, host port maps for 8080/8443)
   - Makefile with targets: up, down, status, demo-cut, demo-restore
   - bootstrap/argocd-install.sh (helm install argocd, wait for ready, print
     URL + admin password)
   - bootstrap/root-app.yaml (the App-of-Apps Application pointing at
     argocd/platform and argocd/workloads)
   Verify: `make up` brings up the cluster, ArgoCD reachable at
   argocd.127-0-0-1.nip.io:8080.

2. Topology spec + render tool
   - spec/atlanta.yaml — the 12-node District-rich topology with all lat/lons,
     prefix-SIDs, link list, agency/circuit/cabinet metadata. Use the data from
     §10 of PLAN.md as the source.
   - tools/render/ — Go-based renderer. Reads spec, emits:
     (a) per-node SR Linux startup configs
     (b) per-cabinet FRR configs
     (c) gNMIc target list with tags
     (d) NetBox seed JSON payload
     (e) links.geojson
   Verify: `go run ./tools/render` produces all five outputs cleanly.

3. NetBox + CNPG + seed Job
   - argocd/platform/cnpg-operator.yaml
   - argocd/platform/netbox.yaml
   - workloads/netbox/cnpg-cluster.yaml, chart-values.yaml, seed-job.yaml
   Verify: NetBox UI loads, seed Job completes, all 12 devices visible with
   sites, cables, agency tenants, ITS service inventory custom fields.

4. Clabernetes Topology + rendered configs
   - argocd/platform/clabernetes.yaml
   - workloads/topology/topology.yaml (Clabernetes Topology CR)
   - workloads/topology/startup-configs/ (rendered per-node)
   Verify: 12 pods up, IS-IS converges across the 8 SR Linux backbone, FRR
   cabinets establish eBGP to their parent hubs.

5. gNMIc + Prometheus + Loki + dashboards
   - argocd/platform/kube-prometheus-stack.yaml, loki.yaml, alloy.yaml
   - workloads/gnmic/ (deployment, configmap with rendered targets, service)
   - workloads/observability/ (rules, alertmanager config, dashboards)
   Verify: Geomap dashboard shows 12 markers in their Atlanta locations,
   links rendered between them, all green. Loki has logs from all pods.

6. Eventing + enriched workflow
   - argocd/platform/argo-workflows.yaml, argo-events.yaml
   - workloads/eventing/ (eventsource, sensors, WorkflowTemplates, Python
     scripts for enrich/analyze/notify)
   Verify: end-to-end demo loop. `make demo-cut INTERFACE=ethernet-1/1
   NODE=tmc-1` triggers an alert, fires the workflow, posts a rich Slack
   message with NetBox enrichment, DOM diagnosis, agency rollup, and
   recommended action.

Workflow expectations:
- Build incrementally. After each step, verify it works before moving on.
- Don't try to do everything at once. Stop and check in at each verification gate.
- If you hit an architectural decision not covered in PLAN.md, ask before
  proceeding.
- Prefer minimal-but-correct over comprehensive. Polish later.
- Use git commits at each meaningful checkpoint with conventional commit messages.

Start with step 1 (bootstrap layer). Read PLAN.md first, then propose the
file structure and contents you'd create. I'll review before you write
anything.
```

### Workflow tips

- **Pull this file fresh each time you change the plan.** If you update PLAN.md
  in conversation here, re-download and replace `docs/PLAN.md` in your repo.
- **Commit checkpoints.** Tag a commit after each verification gate
  (`git tag v0.1-bootstrap`, etc.). Easy to roll back if a later step
  destabilizes things.
- **Keep `docs/decisions.md`.** Any architectural call Claude Code makes that
  isn't in PLAN.md, have it record there. Audit trail for why things are how
  they are.
- **Re-prime as needed.** If a Claude Code session goes long and starts to
  drift, paste a "re-read docs/PLAN.md and confirm we're on step N" message to
  reground it.

### What this kickoff prompt does *not* do

- Doesn't make any of the open decisions in §11 — Claude Code should ask about
  TLS, sealed-secrets vs SOPS, NetBox seed depth, DOM faking strategy, etc.
- Doesn't pin specific chart versions — let Claude Code pick latest stable for
  cert-manager, kube-prometheus-stack, NetBox chart, etc.
- Doesn't presume your Slack workspace or webhook URL — Claude Code will ask
  for those when it gets to step 6.

---

## 16. Build progress

Tracking against the §15 kickoff prompt's six-step plan. Update on every
verification gate; tag the matching commit so rollbacks are clean.

| Step | Description | Status | Tag |
|---|---|---|---|
| 1 | Bootstrap layer (k3d, Makefile, ArgoCD install, App-of-Apps root) | ✅ Done | `v0.1-bootstrap` |
| 2 | Topology spec + Go renderer (5 outputs) | ✅ Done | `v0.2-render` |
| 3 | NetBox + CNPG Postgres + Valkey + seed Job | ✅ Done | `v0.3-netbox` |
| 4 | Clabernetes Topology + rendered configs | ⏳ Not started | — |
| 5 | gNMIc + Prometheus + Loki + dashboards | ⏳ Not started | — |
| 6 | Eventing + enriched workflow | ⏳ Not started | — |

### Deviations from the original plan

Decisions made during the build that depart from PLAN as originally written.
Listed here so the rest of PLAN can stay as the architectural reference while
the as-built reality is honest.

- **Atlas DOT (ADOT) instead of Georgia DOT (GDOT).** Replaced the GDOT framing
  with a fictional state DOT named **Atlas DOT** (Region 7 Atlanta Metro), and
  replaced all real agency / fiber-provider names with fictional ones (Capitol
  Metro Public Works, Northridge Transportation Authority, Pinecrest Public
  Works, Apex Fiber Networks, Cascade Telecom Group, etc.). Real Atlanta
  geography — cities, interstates, lat/lon — kept for credibility. Affects
  every step from §10 onward.

- **NetBox stack — operators + non-Bitnami runtime (step 3).** §4 listed CNPG
  operator + bundled Bitnami Redis. The earlier "Bitnami unavailable" memory
  turned out to be wrong (the OCI catalog and bitnami/* images both still
  resolve), but the user's preference is to avoid Bitnami runtime regardless,
  and there's no Redis subchart anymore — upstream switched to Valkey.
  Resolved as: keep CNPG (it's non-Bitnami), bring in `valkey-io/valkey-helm`
  (the standalone `valkey` chart, image `valkey/valkey`), and run the upstream
  `netbox-community/netbox-chart` with `postgresql.enabled=false` and
  `valkey.enabled=false` so the only Bitnami artifact in the dep tree is the
  helper-only `bitnamicharts/common` template library (zero workloads, zero
  images contributed). Postgres uses ephemeral storage per user direction.

- **cert-manager landed in step 3 instead of being deferred.** PLAN listed
  cert-manager at platform sync-wave -1 but didn't say which step would land
  it. NetBox ingress was opted into TLS in step 3 (self-signed ClusterIssuer,
  Traefik class, host `netbox.127-0-0-1.nip.io:8443`), which forced
  cert-manager into the same step.

- **Step 3 sync-wave numbering.** -1 cert-manager · 0 CNPG operator · 1 Valkey
  StatefulSet · 2 NetBox prereqs (ClusterIssuer + CNPG Cluster CR) · 3 NetBox
  chart · 4 NetBox seed Job. ArgoCD child Applications carry the wave on their
  metadata; resources within each Application inherit it.

- **Seed Job is a Python script, not pynetbox.** The seed payload uses
  human-friendly slug/name FK references (e.g. `device.site = "forest-park"`)
  that NetBox's API doesn't accept directly. The seed Job runs a stdlib-only
  Python script (`workloads/netbox/seed/seed.py`) on `python:3.12-slim` that
  walks the JSON, resolves FKs to numeric IDs via lookup-by-slug, and POSTs
  to the API in dependency order. Idempotent: skips items that already exist
  by slug/name. seed.json moved from `workloads/netbox/seed.json` to
  `workloads/netbox/seed/seed.json` so kustomize's configMapGenerator can
  pick it up without `--load-restrictor` overrides; renderer updated to match.

- **ArgoCD chart pin.** Bootstrap installs `argo-cd` chart `9.5.13` rather than
  unpinned latest, set via `bootstrap/argocd-install.sh` and overridable through
  `ARGOCD_CHART_VERSION`. PLAN didn't prescribe a version.

- **Renderer is plain Go, not templated.** §6 mentions a render tool generally;
  the actual `tools/render/` is a small Go program (single dependency:
  `gopkg.in/yaml.v3`) that emits the five PLAN-specified outputs by direct
  `fmt.Fprintf`. Outputs are committed to `workloads/*` so ArgoCD syncs what's
  in git — no init containers, no deploy-time render.
