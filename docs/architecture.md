# Architecture

This is a single-laptop GitOps demo of a metro DWDM/IS-IS network with
multi-source telemetry and event-driven impact analysis. The fictional
operator is **Atlas DOT, Region 7 — Atlanta Metro**.

The point is to show how the *operational story* — alert fires, impact is
analyzed, the right humans get paged with structured context — falls out
of standard CNCF tools when they're wired together carefully.

## Topology

```
spec/atlanta.yaml   ──►   tools/render/   ──►   workloads/{topology,gnmic,...}
```

`spec/atlanta.yaml` is the single source of truth: 12 nodes, 15 links, 11
agencies. The Go renderer (`tools/render/`, ~zero deps) emits everything
downstream — SR Linux startup configs, FRR daemon configs, gNMIc target
list, the clabernetes Topology CR, the NetBox seed JSON, link-membership
recording rules, snmpd.conf, the dom-synth links file, and a GeoJSON of
the cable plant.

Run `make render` after editing the spec. Nothing in the cluster is
hand-authored.

### Roles

| Role | Count | Kind | Notes |
|---|---|---|---|
| `tmc` (Traffic Mgmt Center) | 2 | SR Linux | Backbone routers, run iBGP between TMCs |
| `corridor-hub` | 6 | SR Linux | Ring routers, terminate the FOC ring + fan out to cabinets |
| `field-cabinet` | 4 | FRR (linux kind) | "Legacy edge" — eBGP to corridor-hub, only SNMP for telemetry |

The 11 backbone links form a closed FOC ring across the 8 SR Linux nodes.
The 4 cabinet links each attach an FRR cabinet to one corridor-hub.

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
  │     enrich   — NetBox lookup (token from secretKeyRef)           │
  │     analyze  — cable graph walk → downstream + agencies          │
  │     notify   — Block Kit → Slack (or stderr if no creds)         │
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

## Telemetry — the gNMI / SNMP split

The fictional Atlas DOT runs a mixed fleet: modern SR Linux backbone, FRR
cabinets at the edge that haven't been refreshed yet. Same demo, two
telemetry pipelines:

```
SR Linux  ──► gNMI subscribe (5s if-state, 10s counters) ──► gNMIc :9804
                                                              │
FRR/Linux ──► SNMP poll every 30s ──────► snmp_exporter ──────┼──► Prometheus
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
              ├─ enrich    : NetBox lookup
              ├─ analyze   : cable graph + agency mapping + severity
              └─ notify    : Block Kit Slack (or stderr if no creds)
                              + Valkey ledger update (resolve closes the thread)
```

Three properties make this work in a demo setting:

1. **Alert gating uses `link_membership_info`**, not `admin_state == 1`.
   `make demo-cut` admin-disables an interface, which would defeat an
   admin-state gate. Joining the alert expression against
   `link_membership_info` filters out the 200+ unused IXR-D3 ports
   without filtering out the cut interface.

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
├── platform/        # third-party charts (kps, loki, alloy, argo-{events,wf}, ...)
└── workloads/       # this-repo manifests (topology, gnmic, observability, snmp,
                     #   eventing, netbox*, dom-synth)
```

The root App-of-Apps points at both directories. Each Application listed
there owns one logical chunk. The `topology` and `snmp` Applications
carry `ignoreDifferences` blocks so clabernetes' admission webhook
defaults and the Prometheus operator's stored `action: replace` don't
register as drift.

## Trade-offs / scope

- **No real SR-MPLS.** The public `ghcr.io/nokia/srlinux` image doesn't
  advertise the `mpls` or `segment-routing` base features on any 7220
  IXR chassis; the YANG containers are `if-feature`-gated. Lab runs IS-IS
  / IPv4 only. The narrative still positions the topology as SR-MPLS in
  *design* — runtime forwarding is plain L3.
- **No real SFPs.** Optical metrics are synthetic (see `dom-synth`).
- **No real Slack.** `notify.py` short-circuits to stderr when the
  `slack-bot` Secret is absent. See `SECRETS.md`.
- **Single-laptop scale.** k3d, 1 server + 2 agents. CNPG is a single
  Postgres pod; Loki is SingleBinary; Prometheus is 6h retention. None
  of this is HA. It's all on purpose.
