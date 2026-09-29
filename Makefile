.PHONY: help doctor preflight up demo wait-ready urls down status render render-check build fix-host-dns repoint last-notify \
        _require_cut_vars demo-cut demo-restore demo-cut-cabinet demo-restore-cabinet demo-cut-fiber demo-restore-fiber \
        scenario-list scenario-hurricane scenario-backhoe scenario-cabinet scenario-flap \
        scenario-gray-failure scenario-gray-failure-end \
        maintenance-start maintenance-end maintenance-list \
        remediation-mode remediation-approve remediation-status \
        drift-check postmortem measure measure-gray ready

# The cluster name is NOT configurable: k3d/config.yaml (metadata.name), the
# registry name (atlas-demo-registry) and the rendered manifests all hardcode
# atlas-demo. `override` stops a stray `make CLUSTER_NAME=...` from making
# `up`/`down` disagree with the config file.
override CLUSTER_NAME := atlas-demo
TOPO_NS      ?= clabernetes
INOTIFY_MIN  ?= 512

# demo-cut authenticates to the device as a NAMED operator (not admin/root)
# so the change is attributed in the AAA syslog → Loki. The Set goes through
# the gnmic pod, which already has reachability to every node's gNMI on 57400.
TOPO_NAME    ?= atlanta
MON_NS       ?= monitoring
GNMI_PORT    ?= 57400
NOC_USER     ?= noc-ops
NOC_PASS     ?= NocOps1!

# Image builds must use a docker-driver buildx builder: a docker-container
# builder runs BuildKit in its own container, where localhost:5001 is not the
# host's k3d registry, so --push fails. Every docker context has a same-named
# docker-driver builder (default / desktop-linux / orbstack), so pin to it.
BUILDER      ?= $(shell docker context show 2>/dev/null || echo default)
REGISTRY     ?= localhost:5001

help:
	@echo "Setup:"
	@echo "  demo         One shot: up + wait-ready + urls (OPEN=1 also opens the console in a browser)"
	@echo "  doctor       Check host prerequisites (tools, Docker memory/CPU, ports, disk, DNS, inotify)"
	@echo "  preflight    Alias for doctor"
	@echo "  up           Doctor + create k3d cluster (skipped if it exists) + build images + ArgoCD + root app"
	@echo "  wait-ready   Re-run the readiness gate every 20s until READY (TIMEOUT= seconds, default 1500)"
	@echo "  urls         Print every UI URL with its credentials (fetches the ArgoCD admin password)"
	@echo "  status       Show node + ArgoCD application state, then the URL table"
	@echo "  down         Delete the k3d cluster"
	@echo "  repoint      Point ArgoCD at your fork: REPO= (default: origin) REV= (default: current branch)"
	@echo "  last-notify  Show the newest enriched-notify workflow's notify output (Slack payload if unconfigured)"
	@echo "Dev:"
	@echo "  render       Re-render workloads/* outputs from spec/atlanta.yaml"
	@echo "  render-check Re-render to /tmp/render-check and verify no drift vs the committed outputs"
	@echo "  build        Build + push the pre-baked images to the k3d registry (localhost:5001; BUILDER= to override)"
	@echo "  fix-host-dns Restore host.k3d.internal resolution in cluster DNS (k3s rewrites NodeHosts and drops it)"
	@echo "Demo:"
	@echo "  demo-cut             Disable an interface on an SR Linux node (NODE=, INTERFACE= required)"
	@echo "  demo-restore         Re-enable an interface on an SR Linux node (NODE=, INTERFACE= required)"
	@echo "  demo-cut-cabinet     Carrier-loss on an FRR cabinet uplink (NODE=, INTERFACE= required) — fires CabinetInterfaceOperDown"
	@echo "  demo-restore-cabinet Restore carrier on an FRR cabinet uplink (NODE=, INTERFACE= required)"
	@echo "  demo-cut-fiber       Real fiber cut on an SR Linux link — carrier loss, admin stays up (NODE=, INTERFACE=)"
	@echo "  demo-restore-fiber   Restore a fiber-cut SR Linux link (NODE=, INTERFACE= required)"
	@echo "  scenario-list        List the canned demo outage scenarios"
	@echo "  scenario-hurricane   Two ring segments fail in series, ~2.5 min"
	@echo "  scenario-backhoe     One random backbone strand cut for ~2 min"
	@echo "  scenario-cabinet     Field cabinet uplink failure, ~1.5 min"
	@echo "  scenario-flap        Trip SRLInterfaceFlapping via rapid up/down, ~3 min"
	@echo "  scenario-gray-failure       Ramp Rx power down + synth errors up on LINK= (warning-severity)"
	@echo "  scenario-gray-failure-end   Clear the gray-failure key for LINK= early"
	@echo "  maintenance-start    Open a maintenance window for NODE= for HOURS= (default 2). Silences alerts."
	@echo "  maintenance-end      Close the maintenance window for NODE= early."
	@echo "  maintenance-list     Show currently active atlas-maintenance silences."
	@echo "  remediation-mode     Set closed-loop remediation mode (MODE=auto|gated)"
	@echo "  remediation-approve  Approve a pending gated remediation (LINK= required)"
	@echo "  remediation-status   Show remediation mode and active cost-outs"
	@echo "  drift-check          Run the config drift audit now (CronWorkflow runs it every 5m)"
	@echo "  postmortem           List stored postmortems, or print+save one (FP=<fingerprint>)"
	@echo "  measure              Run N cut->detect->notify cycles, emit CSV+stats (N=, LANE=gnmi|snmp)"
	@echo "  ready                Functional readiness gate (telemetry/eventing/cabinets), exits non-zero if not ready"
	@echo "  measure-gray         Gray-failure detectability sweep: streaming vs polling vs traps (DURATIONS=)"

up: doctor
	@if k3d cluster list $(CLUSTER_NAME) >/dev/null 2>&1; then \
	  echo "==> k3d cluster '$(CLUSTER_NAME)' already exists — skipping create"; \
	  if k3d cluster list $(CLUSTER_NAME) --no-headers 2>/dev/null | awk '{split($$2,s,"/"); exit !(s[1] < s[2])}'; then \
	    echo "==> Cluster is stopped — starting it"; k3d cluster start $(CLUSTER_NAME) || exit 1; \
	  fi; \
	  k3d kubeconfig merge $(CLUSTER_NAME) --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null || exit 1; \
	else \
	  echo "==> Creating k3d cluster '$(CLUSTER_NAME)'"; \
	  k3d cluster create -c k3d/config.yaml; \
	fi
	@echo "==> Building + pushing pre-baked images"
	@$(MAKE) --no-print-directory build
	@echo "==> Installing ArgoCD"
	bash bootstrap/argocd-install.sh
	@echo "==> Applying root Application (App-of-Apps)"
	kubectl apply -f bootstrap/root-app.yaml
	@$(MAKE) --no-print-directory status
	@echo
	@echo "==> Bootstrapped. Apps take ~10-20 min to converge: 'make wait-ready' blocks until the lab is demo-ready."

# Host prerequisites. Hard failures (missing tools, Docker down, ports taken)
# exit non-zero and abort `make up`; resource/DNS/inotify shortfalls warn only.
doctor:
	@CLUSTER_NAME=$(CLUSTER_NAME) INOTIFY_MIN=$(INOTIFY_MIN) bin/doctor.sh

preflight: doctor

# One shot for a fresh machine: bootstrap, block until functionally ready,
# print the URL table. OPEN=1 also opens the scenario console.
demo:
	@$(MAKE) --no-print-directory up
	@$(MAKE) --no-print-directory wait-ready
	@$(MAKE) --no-print-directory urls
	@if [ "$(OPEN)" = "1" ]; then \
	  url=http://console.127-0-0-1.nip.io:8080; \
	  if [ "$$(uname -s)" = Darwin ]; then open "$$url"; \
	  elif command -v wslview >/dev/null 2>&1; then wslview "$$url"; \
	  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$$url" >/dev/null 2>&1 & \
	  else echo "==> open $$url in a browser"; fi; \
	fi

wait-ready:
	@TIMEOUT=$(or $(TIMEOUT),1500) INTERVAL=$(or $(INTERVAL),20) bin/wait-ready.sh

# Every UI behind the Traefik ingress (:8080 plain HTTP avoids the
# self-signed TLS warning; :8443 serves the same hosts over HTTPS).
urls:
	@if kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then \
	  pw=$$(kubectl -n argocd get secret argocd-initial-admin-secret -o go-template='{{.data.password | base64decode}}' 2>/dev/null); \
	elif kubectl -n argocd get deploy argocd-server >/dev/null 2>&1; then \
	  pw="(initial secret deleted — use the password you set)"; \
	else \
	  pw="(ArgoCD not installed yet — run make up)"; \
	fi; \
	echo "==> UIs (http://…:8080; same hosts on https://…:8443)"; \
	printf '  %-12s %-44s %s\n' "UI" "URL" "Login"; \
	printf '  %-12s %-44s %s\n' "ArgoCD"      "http://argocd.127-0-0-1.nip.io:8080"      "admin / $$pw"; \
	printf '  %-12s %-44s %s\n' "NetBox"      "http://netbox.127-0-0-1.nip.io:8080"      "admin / admin"; \
	printf '  %-12s %-44s %s\n' "Grafana"     "http://grafana.127-0-0-1.nip.io:8080"     "admin / admin"; \
	printf '  %-12s %-44s %s\n' "Workflows"   "http://workflows.127-0-0-1.nip.io:8080"   "no auth (server mode)"; \
	printf '  %-12s %-44s %s\n' "Clabernetes" "http://clabernetes.127-0-0-1.nip.io:8080" "no auth"; \
	printf '  %-12s %-44s %s\n' "Console"     "http://console.127-0-0-1.nip.io:8080"     "no auth (scenario console)"

# Point ArgoCD at a fork/branch. Rewrites the repo URL + revision in the two
# files that name this repo; ArgoCD reads them from the REMOTE, so commit and
# push afterwards. Chart sources ({{ .chart.* }} templates) are left alone.
repoint:
	@repo='$(REPO)'; rev='$(REV)'; \
	[ -n "$$repo" ] || repo=$$(git remote get-url origin 2>/dev/null); \
	[ -n "$$repo" ] || { echo "no REPO= given and no 'origin' remote" >&2; exit 1; }; \
	[ -n "$$rev" ] || rev=$$(git rev-parse --abbrev-ref HEAD 2>/dev/null); \
	[ -n "$$rev" ] && [ "$$rev" != HEAD ] || { echo "detached HEAD — pass REV=<branch>" >&2; exit 1; }; \
	case "$$repo" in \
	  git@*:*) repo=$$(printf '%s' "$$repo" | sed -E 's#^git@([^:]+):#https://\1/#') ;; \
	  ssh://git@*) repo=$$(printf '%s' "$$repo" | sed -E 's#^ssh://git@([^/:]+)(:[0-9]+)?/#https://\1/#') ;; \
	esac; \
	echo "==> Repointing ArgoCD to $$repo @ $$rev"; \
	REPO="$$repo" REV="$$rev" perl -pi -e \
	  's{^(\s*-?\s*repoURL:\s*)(?!.*\{\{)\S+}{$$1$$ENV{REPO}}; s{^(\s*-?\s*(?:targetRevision|revision):\s*)(?!.*\{\{)\S+}{$$1$$ENV{REV}}' \
	  bootstrap/root-app.yaml argocd/applicationset.yaml; \
	grep -nE 'repoURL:|targetRevision:|revision:' bootstrap/root-app.yaml argocd/applicationset.yaml | grep -v '{{' | sed 's/^/    /'; \
	echo "==> Now commit + push these two files to $$rev on $$repo — ArgoCD syncs from the remote, not this checkout:"; \
	echo "      git add bootstrap/root-app.yaml argocd/applicationset.yaml && git commit -m 'chore: repoint ArgoCD' && git push"; \
	echo "    Already bootstrapped? Re-apply the root app: kubectl apply -f bootstrap/root-app.yaml"; \
	echo "    (A private repo also needs ArgoCD repo credentials.)"

# Newest enriched-notify run's notify step. Without the slack-bot Secret,
# notify.py prints the Block Kit payload it WOULD post to stderr, so the pod
# log is the payload. Falls back to the stored step result if the pod is gone.
last-notify:
	@wf=$$(kubectl -n argo-events get workflows.argoproj.io --sort-by=.metadata.creationTimestamp \
	  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep '^enrich-notify-' | tail -1); \
	[ -n "$$wf" ] || { echo "no enriched-notify workflows yet — trigger one: make scenario-backhoe (or demo-cut-fiber)"; exit 1; }; \
	phase=$$(kubectl -n argo-events get workflows.argoproj.io "$$wf" -o jsonpath='{.status.phase}'); \
	echo "==> $$wf ($$phase)"; \
	pod=$$(kubectl -n argo-events get pods -l workflows.argoproj.io/workflow="$$wf" \
	  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.workflows\.argoproj\.io/node-name}{"\n"}{end}' 2>/dev/null \
	  | awk -F'\t' '$$2 ~ /\.notify(\([0-9]+\))?$$/ {print $$1}' | tail -1); \
	if [ -n "$$pod" ]; then \
	  echo "==> kubectl -n argo-events logs $$pod -c main"; \
	  kubectl -n argo-events logs "$$pod" -c main; \
	else \
	  echo "==> notify pod not found (not started yet, or garbage-collected) — stored step result:"; \
	  out=$$(kubectl -n argo-events get workflows.argoproj.io "$$wf" -o go-template='{{range .status.nodes}}{{if eq .displayName "notify"}}phase: {{.phase}}{{"\n"}}{{with .outputs}}{{with .result}}{{.}}{{end}}{{end}}{{"\n"}}{{end}}{{end}}'); \
	  if [ -n "$$out" ]; then printf '%s\n' "$$out"; else echo "  (notify step has not run yet)"; fi; \
	fi

down:
	k3d cluster delete $(CLUSTER_NAME)

render:
	go run ./tools/render -spec spec/atlanta.yaml -out .

render-check:
	@echo "==> Re-rendering to /tmp/render-check"
	@rm -rf /tmp/render-check
	@mkdir -p /tmp/render-check/workloads/observability/dashboards
	@cp workloads/observability/dashboards/*.json /tmp/render-check/workloads/observability/dashboards/ 2>/dev/null || true
	@go run ./tools/render -spec spec/atlanta.yaml -out /tmp/render-check >/dev/null
	@echo "==> Checking renderer-emitted files for drift"
	@drift=0; \
	files=" \
	  workloads/observability/link-membership.yaml \
	  workloads/observability/link-rate-rules.yaml \
	  workloads/gnmic/targets.yaml \
	  workloads/snmp/probe.yaml \
	  workloads/topology/topology.yaml \
	  workloads/topology/kustomization.yaml \
	  workloads/eventing/wft-cut-fiber.yaml \
	  workloads/eventing/wft-incident-collector.yaml \
	  workloads/eventing/wft-enriched-notify.yaml \
	  workloads/eventing/wft-maintenance.yaml \
	  workloads/eventing/wft-remediation.yaml \
	  workloads/eventing/wft-drift-audit.yaml \
	  workloads/eventing/wft-ai-analyst.yaml \
	  workloads/eventing/wft-gray-failure.yaml \
	  workloads/eventing/scripts/drift_expected.json \
	  workloads/versions.yaml \
	  workloads/netbox/seed/seed.json \
	  workloads/dom-synth/links.json \
	  tools/console/static/console-targets.json \
	"; \
	for f in $$files \
	         $$(ls workloads/topology/startup-configs/* 2>/dev/null) \
	         $$(ls workloads/observability/dashboards/*.json 2>/dev/null); do \
	  if [ ! -f "/tmp/render-check/$$f" ]; then \
	    echo "MISSING in render-check: $$f" >&2; drift=1; continue; \
	  fi; \
	  if ! diff -q "/tmp/render-check/$$f" "$$f" >/dev/null 2>&1; then \
	    echo "DRIFT: $$f" >&2; \
	    diff -u "$$f" "/tmp/render-check/$$f" | head -20 >&2; \
	    drift=1; \
	  fi; \
	done; \
	if [ $$drift -eq 1 ]; then \
	  echo "==> DRIFT detected — hand-edits to renderer outputs must go back to tools/render/" >&2; \
	  exit 1; \
	fi; \
	echo "==> Banner check"; \
	for f in $$files \
	         $$(ls workloads/topology/startup-configs/* 2>/dev/null) \
	         $$(ls workloads/observability/dashboards/*.json 2>/dev/null); do \
	  case "$$f" in *.json) continue ;; esac; \
	  if ! head -2 "$$f" | grep -q "Generated by tools/render"; then \
	    echo "MISSING BANNER: $$f" >&2; drift=1; \
	  fi; \
	done; \
	if [ $$drift -eq 1 ]; then exit 1; fi; \
	echo "==> render-check OK"

# frr-snmpd is intentionally not built: the topology runs the stock
# quay.io/frrouting/frr image (see tools/render/constants.go). Its Dockerfile
# stays in images/frr-snmpd/ for a future pull-through fix.
build:
	@echo "==> Building + pushing pre-baked demo images to localhost:5001"
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo "docker not found on host — required for 'make build'" >&2; \
	  exit 1; \
	fi
	@if ! docker buildx ls >/dev/null 2>&1; then \
	  echo "docker buildx not available — required for 'make build'" >&2; \
	  exit 1; \
	fi
	@echo "    (buildx builder: $(BUILDER))"
	docker buildx build --builder $(BUILDER) -t $(REGISTRY)/eventing-py:latest -f images/eventing-py/Dockerfile workloads/eventing/ --push
	docker buildx build --builder $(BUILDER) -t $(REGISTRY)/dom-synth:latest   -f images/dom-synth/Dockerfile   workloads/dom-synth/ --push
	docker buildx build --builder $(BUILDER) -t $(REGISTRY)/ai-analyst:latest  -f images/ai-analyst/Dockerfile  workloads/eventing/ --push
	docker buildx build --builder $(BUILDER) -t $(REGISTRY)/chat-agent:latest  -f images/chat-agent/Dockerfile  workloads/eventing/ --push
	docker buildx build --builder $(BUILDER) -t $(REGISTRY)/console:latest     -f images/console/Dockerfile     .                    --push
	@echo "==> All images pushed. Verify with: curl -s $(REGISTRY)/v2/_catalog"

## host.k3d.internal is how the in-cluster AI lanes (analyst + chat) reach a
## model server running on the host (SECRETS.md). k3d injects the name into
## CoreDNS's NodeHosts ConfigMap at cluster create, but k3s OWNS that
## ConfigMap and rewrites it over time, silently dropping the entry — a fresh
## `make up` works, then days later the AI lanes fail with "Connection
## error". This pins the name in a k3s-native coredns-custom server block,
## which the NodeHosts rewrites can't touch. Idempotent; safe to re-run.
fix-host-dns:
	@IP=$$(docker run --rm --add-host=host.docker.internal:host-gateway alpine \
	  getent ahostsv4 host.docker.internal | awk 'NR==1{print $$1}'); \
	if [ -z "$$IP" ]; then \
	  echo "could not detect the docker host-gateway IP" >&2; exit 1; \
	fi; \
	echo "==> Pinning host.k3d.internal -> $$IP in kube-system/coredns-custom"; \
	VAL=$$(printf 'host.k3d.internal:53 {\n    hosts {\n        %s host.k3d.internal\n        fallthrough\n    }\n}\n' "$$IP"); \
	kubectl -n kube-system create configmap coredns-custom \
	  --from-literal=hostk3d.server="$$VAL" \
	  --dry-run=client -o yaml | kubectl apply -f -; \
	kubectl -n kube-system rollout restart deploy/coredns; \
	kubectl -n kube-system rollout status deploy/coredns --timeout=90s

status:
	@echo "==> Nodes"
	@kubectl get nodes 2>/dev/null || echo "  (cluster not running)"
	@echo
	@echo "==> ArgoCD applications"
	@kubectl -n argocd get applications.argoproj.io 2>/dev/null || echo "  (none yet)"
	@echo
	@$(MAKE) --no-print-directory urls

# --- Failure injection (functional once the Clabernetes topology is deployed in step 4) ---

_require_cut_vars:
	@[ -n "$(NODE)" ]      || { echo "NODE is required (e.g. NODE=tmc-1)"; exit 1; }
	@[ -n "$(INTERFACE)" ] || { echo "INTERFACE is required (e.g. INTERFACE=ethernet-1/1)"; exit 1; }

## Clabernetes runs each lab node as a nested docker container inside the
## launcher pod, so all sr_cli / vtysh invocations have to docker exec into
## that inner container — kubectl exec lands in the launcher's docker daemon,
## not the SR Linux / FRR process.

# admin-state disable/enable as the named operator noc-ops, via a gNMI Set
# through the gnmic pod. The device logs "committed ... by user noc-ops from
# host <gnmic-ip>" (sr_aaa_mgr / sr_mgmt_server → Loki) — so the audit trail
# and the AI analyst can name WHO made the change, not just that it happened.
demo-cut: _require_cut_vars
	@echo "==> Disabling $(INTERFACE) on $(NODE) — gNMI Set as $(NOC_USER)"; \
	  kubectl -n $(MON_NS) exec deploy/gnmic -- /app/gnmic \
	    -a $(TOPO_NAME)-$(NODE).$(TOPO_NS).svc.cluster.local:$(GNMI_PORT) \
	    -u $(NOC_USER) -p '$(NOC_PASS)' --skip-verify -e json_ietf \
	    set --update-path '/interface[name=$(INTERFACE)]/admin-state' --update-value disable

demo-restore: _require_cut_vars
	@echo "==> Enabling $(INTERFACE) on $(NODE) — gNMI Set as $(NOC_USER)"; \
	  kubectl -n $(MON_NS) exec deploy/gnmic -- /app/gnmic \
	    -a $(TOPO_NAME)-$(NODE).$(TOPO_NS).svc.cluster.local:$(GNMI_PORT) \
	    -u $(NOC_USER) -p '$(NOC_PASS)' --skip-verify -e json_ietf \
	    set --update-path '/interface[name=$(INTERFACE)]/admin-state' --update-value enable

# --- FRR cabinet failure injection (legacy-edge / SNMP-driven demo lane) ---
# Inject a real carrier loss, not an admin shutdown. CabinetInterfaceOperDown
# fires on ifOperStatus==down AND ifAdminStatus==up (a link failure, not
# maintenance). A vtysh `shutdown` drops admin too, so the alert never fires.
# Downing the pod-side veth (<node>-<iface>, e.g. fc-n-eth1) drops carrier on
# the cabinet interface while admin stays up — the exact condition the alert
# requires. The loss does not cross the VXLAN back to the SR Linux side.

demo-cut-cabinet: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Carrier loss on $(NODE) $(INTERFACE) ($$POD): down pod-side veth $(NODE)-$(INTERFACE)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- ip link set $(NODE)-$(INTERFACE) down

demo-restore-cabinet: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Restore carrier on $(NODE) $(INTERFACE) ($$POD): up pod-side veth $(NODE)-$(INTERFACE)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- ip link set $(NODE)-$(INTERFACE) up

# --- SR Linux fiber cut (carrier loss) vs the admin-disable demo-cut ------
# demo-cut sets admin-state=disable — a maintenance shutdown the AI analyst
# correctly reads as a deliberate config action. For a REAL fault, down the
# pod-side veth (<node>-e1-<x>, e.g. hub-e-e1-2 for ethernet-1/2): the SR Linux
# interface goes oper-down while admin-state stays ENABLE, with a physical
# oper-down-reason — a fiber-cut/carrier-loss the analyst diagnoses as a
# hardware/link failure. Leave it cut until the analysis runs (it reasons over
# live state). Same SRLInterfaceOperDown alert either way.

demo-cut-fiber: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  VETH=$(NODE)-$$(echo $(INTERFACE) | sed 's#ethernet-#e#; s#/#-#'); \
	  echo "==> Fiber cut (carrier loss) on $(NODE) $(INTERFACE) ($$POD): down pod veth $$VETH (admin-state stays up)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- ip link set $$VETH down

demo-restore-fiber: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  VETH=$(NODE)-$$(echo $(INTERFACE) | sed 's#ethernet-#e#; s#/#-#'); \
	  echo "==> Restore fiber on $(NODE) $(INTERFACE) ($$POD): up pod veth $$VETH"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- ip link set $$VETH up

# --- Readiness gate -------------------------------------------------------
# Functional readiness (telemetry flowing, eventing wired, cabinets polling),
# not just ArgoCD "Healthy". Exits non-zero if the lab isn't demo-ready.
ready:
	@bin/ready.sh

# --- Measurement harness (paper results table) ---------------------------
# Run N cut->detect->enriched-notify cycles and emit a CSV + summary stats.
# Rotates distinct interfaces by default (independent runs, no cherry-picking).
#   make measure N=10 LANE=gnmi
#   make measure N=10 LANE=snmp
#   make measure N=10 LANE=gnmi IFACES="hub-n:ethernet-1/2 hub-e:ethernet-1/2"
#   make measure N=10 LANE=gnmi NODE=hub-n INTERFACE=ethernet-1/1   # pin one
measure:
	@bin/measure.sh -n $(or $(N),10) -l $(or $(LANE),gnmi) \
	  $(if $(NODE),-N $(NODE)) $(if $(INTERFACE),-i $(INTERFACE)) \
	  $(if $(IFACES),-I "$(IFACES)") $(if $(OUT),-o $(OUT))

# Gray-failure detectability: streaming (measured) vs 5-min polling vs traps.
#   make measure-gray DURATIONS="180 360 600"
measure-gray:
	@bin/measure-gray.sh $(if $(DURATIONS),-D "$(DURATIONS)") $(if $(LINKS),-l "$(LINKS)") \
	  $(if $(POLL),-p $(POLL)) $(if $(OUT),-o $(OUT))

# --- Pre-canned demo scenarios -------------------------------------------

scenario-list:
	@bin/scenarios.sh list

scenario-hurricane:
	@bin/scenarios.sh hurricane

scenario-backhoe:
	@bin/scenarios.sh backhoe

scenario-cabinet:
	@bin/scenarios.sh cabinet-loss

scenario-flap:
	@bin/scenarios.sh flapping

scenario-gray-failure:
	@bin/scenarios.sh gray-failure "$(LINK)"

scenario-gray-failure-end:
	@bin/scenarios.sh gray-failure-end "$(LINK)"

# --- Maintenance windows -------------------------------------------------

maintenance-start:
	@bin/maintenance.sh start "$(NODE)" "$(or $(HOURS),2)" "$(or $(COMMENT),scheduled maintenance)"

maintenance-end:
	@bin/maintenance.sh end "$(NODE)"

maintenance-list:
	@bin/maintenance.sh list

# --- Closed-loop remediation ----------------------------------------------

remediation-mode:
	@[ "$(MODE)" = "auto" ] || [ "$(MODE)" = "gated" ] || { echo "MODE must be auto or gated (e.g. MODE=gated)"; exit 1; }
	@kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 set remediation:mode $(MODE) >/dev/null
	@echo "==> remediation mode: $(MODE)"

remediation-approve:
	@[ -n "$(LINK)" ] || { echo "LINK is required (e.g. LINK=ring-e-i20e)"; exit 1; }
	@kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 set remediation:approve:$(LINK) 1 EX 900 >/dev/null
	@echo "==> approval recorded for $(LINK) (valid 15 minutes)"

remediation-status:
	@printf "mode:   "; kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 get remediation:mode 2>/dev/null | grep . || echo "auto (default)"
	@echo "active:"
	@kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 --scan --pattern 'remediation:active:*' 2>/dev/null | sed 's/^/  /' | grep . || echo "  (none)"

# --- Config drift audit ---------------------------------------------------

drift-check:
	@echo '{"apiVersion":"argoproj.io/v1alpha1","kind":"Workflow","metadata":{"generateName":"drift-check-"},"spec":{"serviceAccountName":"operate-workflow-sa","workflowTemplateRef":{"name":"drift-audit"}}}' \
	  | kubectl -n argo-events create -f -
	@echo "==> drift audit submitted; watch: kubectl -n argo-events get workflows"

# --- Postmortems -----------------------------------------------------------

postmortem:
	@if [ -z "$(FP)" ]; then \
	  echo "Stored postmortems (fetch one: make postmortem FP=<fingerprint>):"; \
	  kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 --scan --pattern 'postmortem:*' 2>/dev/null | sed 's/^postmortem:/  /' | grep . || echo "  (none)"; \
	else \
	  kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 exists postmortem:$(FP) | grep -q 1 || { echo "no postmortem stored for $(FP)"; exit 1; }; \
	  f=/tmp/postmortem-$(FP).md; \
	  kubectl -n valkey exec deploy/valkey -c valkey -- valkey-cli -n 2 get postmortem:$(FP) > $$f; \
	  cat $$f; \
	  echo; echo "==> saved to $$f"; \
	fi
