.PHONY: up preflight down status render render-check build demo-cut demo-restore demo-cut-cabinet demo-restore-cabinet \
        scenario scenario-list scenario-hurricane scenario-backhoe scenario-cabinet scenario-flap \
        scenario-gray-failure scenario-gray-failure-end \
        maintenance-start maintenance-end maintenance-list \
        remediation-mode remediation-approve remediation-status \
        drift-check postmortem measure ready demo-cut-fiber demo-restore-fiber help

CLUSTER_NAME ?= atlas-demo
TOPO_NS      ?= clabernetes
INOTIFY_MIN  ?= 512

help:
	@echo "Targets:"
	@echo "  up           Create k3d cluster + build images + bootstrap ArgoCD + apply root Application"
	@echo "  preflight    Check host fs.inotify.max_user_instances (eventing needs headroom)"
	@echo "  down         Delete the k3d cluster"
	@echo "  status       Show node + ArgoCD application state, print URL and admin password"
	@echo "  render       Re-render workloads/* outputs from spec/atlanta.yaml"
	@echo "  render-check Re-render to /tmp/render-check and verify no drift vs the committed outputs"
	@echo "  build        Build + push the pre-baked images to the k3d registry (localhost:5001)"
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

up: preflight
	@echo "==> Creating k3d cluster '$(CLUSTER_NAME)'"
	k3d cluster create -c k3d/config.yaml
	@echo "==> Building + pushing pre-baked images"
	@$(MAKE) --no-print-directory build
	@echo "==> Installing ArgoCD"
	bash bootstrap/argocd-install.sh
	@echo "==> Applying root Application (App-of-Apps)"
	kubectl apply -f bootstrap/root-app.yaml
	@$(MAKE) --no-print-directory status

preflight:
	@instances=$$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0); \
	if [ "$$instances" -lt $(INOTIFY_MIN) ]; then \
	  echo ""; \
	  echo "  !!  fs.inotify.max_user_instances=$$instances (< $(INOTIFY_MIN))"; \
	  echo "      The argo-events data plane (NATS EventBus + sensors + eventsource)"; \
	  echo "      will crashloop with 'too many open files' and the cut->notify"; \
	  echo "      automation will silently never fire. Raise it (one-time, host):"; \
	  echo ""; \
	  echo "        sudo sysctl fs.inotify.max_user_instances=1024"; \
	  echo "        echo 'fs.inotify.max_user_instances=1024' | sudo tee /etc/sysctl.d/99-inotify.conf"; \
	  echo ""; \
	  echo "      Continuing anyway — the cluster and dashboards still work; only"; \
	  echo "      the eventing pipeline is affected. See docs/runbook-troubleshoot.md."; \
	  echo ""; \
	else \
	  echo "==> preflight: fs.inotify.max_user_instances=$$instances (ok)"; \
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
	docker buildx build -t localhost:5001/eventing-py:latest -f images/eventing-py/Dockerfile workloads/eventing/ --push
	docker buildx build -t localhost:5001/dom-synth:latest   -f images/dom-synth/Dockerfile   workloads/dom-synth/ --push
	docker buildx build -t localhost:5001/frr-snmpd:latest   -f images/frr-snmpd/Dockerfile   images/frr-snmpd/    --push
	docker buildx build -t localhost:5001/ai-analyst:latest  -f images/ai-analyst/Dockerfile  workloads/eventing/ --push
	docker buildx build -t localhost:5001/console:latest     -f images/console/Dockerfile     .                    --push
	@echo "==> All images pushed. Verify with: curl -s localhost:5001/v2/_catalog"

status:
	@echo "==> Nodes"
	@kubectl get nodes 2>/dev/null || echo "  (cluster not running)"
	@echo
	@echo "==> ArgoCD applications"
	@kubectl -n argocd get applications.argoproj.io 2>/dev/null || echo "  (none yet)"
	@echo
	@echo "==> ArgoCD URL:      http://argocd.127-0-0-1.nip.io:8080"
	@echo "==> ArgoCD username: admin"
	@printf  "==> ArgoCD password: "
	@kubectl -n argocd get secret argocd-initial-admin-secret \
		-o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo "(secret not yet created)"
	@echo

# --- Failure injection (functional once the Clabernetes topology is deployed in step 4) ---

_require_cut_vars:
	@[ -n "$(NODE)" ]      || { echo "NODE is required (e.g. NODE=tmc-1)"; exit 1; }
	@[ -n "$(INTERFACE)" ] || { echo "INTERFACE is required (e.g. INTERFACE=ethernet-1/1)"; exit 1; }

## Clabernetes runs each lab node as a nested docker container inside the
## launcher pod, so all sr_cli / vtysh invocations have to docker exec into
## that inner container — kubectl exec lands in the launcher's docker daemon,
## not the SR Linux / FRR process.

demo-cut: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Disabling $(INTERFACE) on $(NODE) ($$POD)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- docker exec $(NODE) bash -c \
	    "echo -e 'enter candidate\nset / interface $(INTERFACE) admin-state disable\ncommit now' | sr_cli"

demo-restore: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Enabling $(INTERFACE) on $(NODE) ($$POD)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- docker exec $(NODE) bash -c \
	    "echo -e 'enter candidate\nset / interface $(INTERFACE) admin-state enable\ncommit now' | sr_cli"

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
