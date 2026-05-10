.PHONY: up down status render demo-cut demo-restore demo-cut-cabinet demo-restore-cabinet help

CLUSTER_NAME ?= atlas-demo
TOPO_NS      ?= clabernetes

help:
	@echo "Targets:"
	@echo "  up           Create k3d cluster + bootstrap ArgoCD + apply root Application"
	@echo "  down         Delete the k3d cluster"
	@echo "  status       Show node + ArgoCD application state, print URL and admin password"
	@echo "  render       Re-render workloads/* outputs from spec/atlanta.yaml"
	@echo "  demo-cut             Disable an interface on an SR Linux node (NODE=, INTERFACE= required)"
	@echo "  demo-restore         Re-enable an interface on an SR Linux node (NODE=, INTERFACE= required)"
	@echo "  demo-cut-cabinet     Disable an interface on an FRR cabinet via vtysh (NODE=, INTERFACE= required)"
	@echo "  demo-restore-cabinet Re-enable an interface on an FRR cabinet via vtysh (NODE=, INTERFACE= required)"

up:
	@echo "==> Creating k3d cluster '$(CLUSTER_NAME)'"
	k3d cluster create -c k3d/config.yaml
	@echo "==> Installing ArgoCD"
	bash bootstrap/argocd-install.sh
	@echo "==> Applying root Application (App-of-Apps)"
	kubectl apply -f bootstrap/root-app.yaml
	@$(MAKE) --no-print-directory status

down:
	k3d cluster delete $(CLUSTER_NAME)

render:
	go run ./tools/render -spec spec/atlanta.yaml -out .

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

demo-cut-cabinet: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Shutting down $(INTERFACE) on $(NODE) ($$POD) via vtysh"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- docker exec $(NODE) \
	    vtysh -c "configure terminal" -c "interface $(INTERFACE)" -c "shutdown" -c "end" -c "write memory"

demo-restore-cabinet: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Bringing up $(INTERFACE) on $(NODE) ($$POD) via vtysh"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- docker exec $(NODE) \
	    vtysh -c "configure terminal" -c "interface $(INTERFACE)" -c "no shutdown" -c "end" -c "write memory"
