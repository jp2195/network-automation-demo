# Troubleshooting runbook

Lookup table by symptom. The interesting / non-obvious bugs are in the
"Subtle gotchas" section at the bottom — read those once before your
first demo.

## Quick triage

```bash
# every layer at a glance
kubectl -n argocd get applications
kubectl -n clabernetes get pods
kubectl -n monitoring get pods
kubectl -n argo-events get pods
kubectl -n netbox get pods

# what alerts are firing
PROM_POD=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- http://localhost:9090/api/v1/alerts | python3 -c "
import sys, json
from collections import Counter
c = Counter(a['labels']['alertname'] for a in json.load(sys.stdin)['data']['alerts'])
for k,v in sorted(c.items()): print(f'  {k}: {v}')"

# recent workflow runs
kubectl -n argo-events get wf --sort-by=.metadata.creationTimestamp | tail -10
```

## Symptom → diagnosis

### "ArgoCD apps stuck OutOfSync after a fresh `make up`"

Almost always the clabernetes admission webhook adding defaults to the
Topology CR (`mode: read` on every `filesFromConfigMap` entry,
`expose.disableExpose: false`, etc.) or the Prometheus operator storing
the implicit `action: replace` on relabeling configs.

These should already be ignored — `argocd/manifests/workloads/{topology,snmp,gnmic}.yaml`
all carry `ignoreDifferences` blocks. If you've added a new app and
hit drift on a Probe / ServiceMonitor / Topology, that's the pattern
to copy.

### "make demo-cut: `sr_cli: executable file not found`"

The Makefile must `kubectl exec POD -- docker exec <node> sr_cli` — the
launcher pod is the docker daemon, the lab node is a nested container
inside it. If you've recently regenerated the Makefile, double-check.

### "Alert never fires"

Walk it backwards.

```bash
# 1. is gnmic actually emitting oper_state=2?
kubectl -n monitoring port-forward svc/gnmic 9804:9804 &
curl -s :9804/metrics | grep '^srl_nokia_interfaces_interface_oper_state{node="<X>",interface="<Y>"'

# 2. is the alert *expression* matching?
PROM_POD=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- --post-data='query=(srl_nokia_interfaces_interface_oper_state == 2) * on(node, interface) group_left(link_id, link_kind) link_membership_info' http://localhost:9090/api/v1/query

# 3. is the alert in pending or firing?
kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- http://localhost:9090/api/v1/alerts | python3 -m json.tool | grep -A3 SRLInterfaceOperDown

# 4. did Alertmanager get it?
AM_POD=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=alertmanager -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec $AM_POD -c alertmanager -- wget -qO- 'http://localhost:9093/api/v2/alerts'

# 5. did the EventSource webhook receive a POST?
kubectl -n argo-events logs -l eventsource-name=webhook --tail=20

# 6. did the Sensor accept it?
kubectl -n argo-events logs -l sensor-name=interface-down --tail=20 | tail
# look for "Triggering actions after receiving dependency alert"
# vs "not interested in dependency alert (didn't pass filter)"
```

If the Sensor logs `not interested in dependency alert (didn't pass filter)`
the alert payload isn't matching the filter. Check the alert is the cut
you intended — the filter only matches `SRLInterfaceOperDown` and
`SRLInterfaceFlapping`. Stock kube-prometheus-stack alerts (`Watchdog`,
`TargetDown`, `CPUThrottlingHigh`) reach the same Alertmanager but get
routed to the `null` receiver.

### "Alert fires but no workflow runs; argo-events pods CrashLoopBackOff"

Symptom: `make demo-cut` fires `SRLInterfaceOperDown` (visible in Alertmanager
and on the dashboards), but `kubectl get workflows -n argo-events` stays empty
and the eventing pods are crashlooping:

```bash
kubectl -n argo-events get pods
# eventbus-default-js-*, interface-down-sensor-*, manual-cut-sensor-*,
# webhook-eventsource-*  all CrashLoopBackOff
kubectl -n argo-events logs -l eventsource-name=webhook --tail=5
# Error: ... "failed to create watcher: too many open files"
```

Root cause: the host `fs.inotify.max_user_instances` limit (default 128) is
exhausted by the full stack, so every fsnotify-using pod (NATS EventBus
reloader, sensors, eventsource) fails to start its watcher. The metric-driven
dashboards still react to a cut, but the enrich→analyze→notify automation
never fires. Fix on the host and let the pods reschedule:

```bash
sudo sysctl fs.inotify.max_user_instances=1024
echo 'fs.inotify.max_user_instances=1024' | sudo tee /etc/sysctl.d/99-inotify.conf
kubectl -n argo-events delete pod -l eventbus-name=default
kubectl -n argo-events delete pod -l sensor-name
kubectl -n argo-events delete pod -l eventsource-name
# within ~10s: eventbus 3/3, sensors + eventsource 1/1; re-cut to verify
```

`make up` runs a `preflight` check that warns when the limit is too low.

### "Workflow created but enrich step failed with `Invalid control character`"

The `description:` annotation on the alert contains literal newlines
(YAML `|` block scalar). When Argo substitutes the alert payload into a
triple-quoted Python string, Python interprets the JSON `\n` as a real
newline, breaking `json.loads`.

Fix in `workloads/eventing/wft-enriched-notify.yaml`: inject the
parameter as `env.value`, not as inline source. The substitution stays
at the YAML-scalar layer.

### "Workflow created but enrich step got 403 from NetBox"

The `argo-events/netbox-api` Secret is missing or has a stale token.

```bash
# is the Secret present?
kubectl -n argo-events get secret netbox-api -o jsonpath='{.data.token}' | base64 -d | head -c 8

# is it valid?
NS_POD=$(kubectl -n netbox get pods -l app.kubernetes.io/component=netbox -o jsonpath='{.items[0].metadata.name}')
TOKEN=$(kubectl -n argo-events get secret netbox-api -o jsonpath='{.data.token}' | base64 -d)
kubectl -n netbox exec $NS_POD -- curl -sS -o /dev/null -w "%{http_code}" \
  -H "Authorization: Token $TOKEN" http://localhost:8080/api/dcim/devices/

# rerun the seed Job to mint + publish a fresh token
kubectl -n netbox delete job netbox-seed --ignore-not-found
kubectl apply -k workloads/netbox/seed
```

### "snmp_exporter probe returns 500, snmpd inside cabinet not listening"

This was a real bug. Two possible root causes — pick by how it fails:

1. **EADDRINUSE on first bind.** wrapper.sh used to call
   `snmpd -c /etc/snmp/snmpd.conf -Lf …`. That `-c` *adds* the file to
   the search path, doesn't replace it; net-snmp's default search
   already includes `/etc/snmp/snmpd.conf`. Result: the same
   `agentaddress` was registered twice, second bind hit EADDRINUSE.
   Fix: drop the `-c` flag (current renderer does this).

2. **Probe times out, snmpd is up.** Wrong port. The cabinet pod's
   container image only publishes the standard SNMP port (161), not
   1161. clabernetes' Service forwards :161 → :161 in the inner
   container; if the renderer somehow pinned 1161, the probe goes
   nowhere. Fix: `agentaddress udp:161` in `snmpd.conf`.

### "Geomap line is grey, not green or red"

The route layer color is read from the `Value` field of a single query
(refId `L`) against the `link_endpoint_geo` recording rule. If that rule
hasn't been picked up, the query returns empty.

```bash
PROM_POD=$(kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- --post-data='query=count(link_endpoint_geo)' http://localhost:9090/api/v1/query
# expected: 30 (15 links × 2 endpoints)
```

If it's 0, force-reload Prometheus:

```bash
kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- --post-data='' http://localhost:9090/-/reload
```

### "DOM panels are flat / empty"

Either dom-synth isn't pumping, or the ServiceMonitor isn't matching.

```bash
kubectl -n monitoring port-forward svc/dom-synth 8000:8000 &
curl -s :8000/metrics | head -10
# expected: dom_temperature_celsius series

kubectl -n monitoring exec $PROM_POD -c prometheus -- wget -qO- 'http://localhost:9090/api/v1/targets' | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
  if 'dom-synth' in t['scrapePool']:
    print(t['health'], t['scrapeUrl'], t.get('lastError','')[:80])
"
```

dom-synth re-reads `links.json` once at startup — if you `make render`'d
new links, you have to bounce the pod for them to register.

## Subtle gotchas

A short collection of things that reliably trip people. Read once.

### gNMIc emits zero `oper_state` metrics

By default. SR Linux returns oper-state as the string `"down"`; gnmic's
prom output drops non-numeric values. The fix is `event-strings` +
`event-convert` processors in the gnmic config (already there) mapping
strings → ints. If you delete those, every interface oper-state
disappears.

### gNMIc's metric name has *one* `srl_` prefix, not two

The renderer used to set `metric-prefix: srl` while gnmic's path
encoding ALSO prepends the YANG module name `srl_nokia`, producing
`srl_srl_nokia_interfaces_*`. Drop the metric-prefix; the YANG module
prefix alone is enough.

### SR Linux ixr-d2l has no MPLS / segment-routing schema

The default chassis type is a TOR. Switch to `type: ixr-d3` (dashed
form) on every nokia_srlinux node in the topology. *Even with ixr-d3,
the public srlinux image doesn't expose the base `mpls`/`segment-routing`
features* — see "Trade-offs / scope" in
[`docs/architecture.md`](architecture.md). Lab is IS-IS / IPv4 only.

### `set / system name <X>` is wrong syntax

It's `set / system name host-name <X>`. `name` is a container, not a
leaf. The renderer (`tools/render/srl_render.go`) gets this right; if
you hand-edit a startup config, copy the existing form.

### `bgp group cabinets export-policy <X>` is wrong syntax

It's `export-policy [<X>]` — leaf-list. Same gotcha shape.

### Alert gating must NOT use `admin_state == 1`

`make demo-cut` admin-disables an interface. If the alert expression
joins on `admin_state == 1`, the cut interface drops out and the alert
never fires. Use `link_membership_info` as the join key instead — it
filters out the 50+ unused IXR-D3 ports per node without filtering out
the cut interface.

### NetBox token can't be hardcoded

NetBox 4.x stores tokens hashed; `tokens/provision/` returns a fresh
plaintext per call. The seed Job mints one and PATCHes it into
`argo-events/netbox-api`. The WFT reads via `secretKeyRef`. Don't try
to bake a token into the WFT — it'll always 403.

### Workflow parameter substitution into Python source corrupts `\n`

```yaml
# DON'T:
source: |
  os.environ["ALERT_JSON"] = """{{inputs.parameters.alert}}"""

# DO:
env:
  - name: ALERT_JSON
    value: "{{inputs.parameters.alert}}"
```

## Hard reset — nuke and pave

If everything is melted:

```bash
# wipes the cluster and recreates it from scratch.
make down
make up

# wait for ArgoCD to sync everything (~5min)
until [ "$(kubectl -n argocd get applications --no-headers | awk '$2=="Synced" && $3=="Healthy"' | wc -l)" -ge 17 ]; do sleep 15; done

# bounce the SR Linux pods once so each picks up its startup-config
for n in tmc-1 tmc-2 hub-n hub-e hub-i20e hub-nw hub-sw hub-i20w; do
  kubectl -n clabernetes delete pod -l clabernetes/topologyOwner=atlanta,clabernetes/topologyNode=$n --wait=false
done

# wait for postdeploy on every node
sleep 90
for n in tmc-1 tmc-2 hub-n hub-e hub-i20e hub-nw hub-sw hub-i20w; do
  P=$(kubectl -n clabernetes get pods -l clabernetes/topologyOwner=atlanta,clabernetes/topologyNode=$n -o jsonpath='{.items[0].metadata.name}')
  kubectl -n clabernetes exec "$P" -- cat /clabernetes/containerlab.log 2>/dev/null | grep -E "postdeploy|ERRO" | tail -1
done
```

Anything firing `ERRO` in the postdeploy log means a startup-config
syntax error. The renderer is the source of truth — re-render and
re-apply the topology if you've edited the spec.

## Postmortem missing after a resolve

`make postmortem` shows nothing for an incident you just resolved:

1. Did the resolved Workflow run? `kubectl -n argo-events get wf | head` —
   look for a recent `enrich-notify-*`. Alertmanager sends the resolved
   webhook on its next group interval, so allow a minute or two.
2. Was the postmortem step Skipped? Check the workflow node states:
   `kubectl -n argo-events get wf <name> -o json | python3 -c "import json,sys; [print(n['displayName'], n['phase']) for n in json.load(sys.stdin)['status']['nodes'].values()]"`.
   Skipped means the notify step reported `status=firing` — either this was
   the firing-path workflow (expected: only the resolved run generates a
   postmortem) or notify could not write `/tmp/argo/status` and the gate
   read its `default: firing`.
3. Step failed? `kubectl -n argo-events logs <pod>` for the postmortem pod —
   a Valkey outage is the only hard failure; Prometheus/Loki problems just
   omit sections. The full Markdown is dumped to the step log on a store
   failure, so the artifact is recoverable from Loki.
4. TTL: postmortems expire after 7 days (`postmortem:<fp>` in Valkey DB 2).

## AI analyst produced nothing

1. **No `ai-analyze-*` workflow at all** — the sensor isn't running or
   eventing isn't synced; same triage as enriched-notify (check
   `kubectl -n argo-events get sensors,pods`).
2. **Workflow Succeeded but the log says `AI disabled`** — the
   `ai-analyst` Secret is absent or incomplete. It needs all three
   keys: `base_url`, `api_key`, `model` (see SECRETS.md).
3. **Workflow Failed** — `kubectl -n argo-events logs <ai-analyze pod>`:
   - connection refused / timeout on `base_url`: for local Ollama,
     confirm it listens beyond loopback (`OLLAMA_HOST=0.0.0.0`) and
     that `host.k3d.internal` resolves from a pod;
   - unknown model name: must match the endpoint's model list;
   - exceeded `activeDeadlineSeconds: 600`: small local models can be
     slow — try a smaller alert window or a stronger model;
   - `Model token limit ... exceeded before any response was generated`:
     a thinking model burned the whole output budget on reasoning —
     set the `reasoning_effort=none` Secret key (SECRETS.md);
   - `The next request would exceed the request_limit`: the model
     investigated without ever submitting. The failed pod's log carries
     a full `--- agent transcript ---` on stderr showing every tool
     call — read it. Usual local-model causes, in order: Ollama context
     too small (`OLLAMA_CONTEXT_LENGTH=16384`; the default ~4k silently
     truncates the conversation and the model loses the thread),
     thinking enabled, temperature too high (set the `temperature=0.2`
     Secret key). All three are baked into the SECRETS.md Ollama recipe.
4. **Postmortem has no "Analyst narrative (AI)" section** — the
   analysis must land in Loki during the incident window (the analyst
   runs on the firing event). Check the marker line exists:
   `kubectl -n argo-events logs <ai-analyze pod> | grep INCIDENT_ANALYSIS_V1`.
   Small local models occasionally fail structured output — rerun the
   incident with a stronger model.

## Drift audit never fires

`kubectl -n argo-events get cwf drift-audit` exists but no
`drift-audit-*` workflows ever appear and `status.lastScheduledTime`
is empty: the cron controller validated the `workflowTemplateRef` in
the instant before its WorkflowTemplate informer saw the `drift-audit`
WFT (a same-sync race on a fresh cluster), marked the CronWorkflow
`SpecError`, and never re-validates on its own. Confirm with
`kubectl -n argo logs deploy/argo-workflows-workflow-controller | grep "invalid cron"`.
Recovery is any update that touches the resource:

    kubectl -n argo-events annotate cronworkflow drift-audit \
      revalidate="$(date +%s)" --overwrite

The manifest carries a `sync-wave: "1"` annotation so ArgoCD applies it
after the WorkflowTemplates, which prevents the race on normal syncs.

## Incident dashboard never appeared / never went away

- **Never appeared** — check the `dashboard` step of the
  `enrich-notify-*` workflow (`kubectl -n argo-events logs <pod>`);
  RBAC errors mean `workloads/incident-dashboards/rbac.yaml`
  isn't applied. The step is non-fatal by design: pipeline success with
  a logged `incident dashboard step failed` line is the signature.
- **Never went away** — the resolve was lost (e.g. eventing was down
  when the alert cleared). Dashboards are plain ConfigMaps:
  `kubectl -n incident-dashboards get cm | grep incident-`
  then `kubectl -n incident-dashboards delete cm incident-<fp>`.
