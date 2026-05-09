.PHONY: up down status render demo-cut demo-restore help

CLUSTER_NAME ?= gdot-demo
TOPO_NS      ?= clabernetes

help:
	@echo "Targets:"
	@echo "  up           Create k3d cluster + bootstrap ArgoCD + apply root Application"
	@echo "  down         Delete the k3d cluster"
	@echo "  status       Show node + ArgoCD application state, print URL and admin password"
	@echo "  render       Re-render workloads/* outputs from spec/atlanta.yaml"
	@echo "  demo-cut     Disable an interface on an SR Linux node (NODE=, INTERFACE= required)"
	@echo "  demo-restore Re-enable an interface on an SR Linux node (NODE=, INTERFACE= required)"

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

demo-cut: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Disabling $(INTERFACE) on $(NODE) ($$POD)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- \
	    sr_cli "enter candidate; set interface $(INTERFACE) admin-state disable; commit now"

demo-restore: _require_cut_vars
	@POD=$$(kubectl -n $(TOPO_NS) get pod -l clabernetes/topologyNode=$(NODE) -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	  if [ -z "$$POD" ]; then echo "no pod for NODE=$(NODE) in ns $(TOPO_NS) - is the topology deployed?"; exit 1; fi; \
	  echo "==> Enabling $(INTERFACE) on $(NODE) ($$POD)"; \
	  kubectl -n $(TOPO_NS) exec $$POD -- \
	    sr_cli "enter candidate; set interface $(INTERFACE) admin-state enable; commit now"
