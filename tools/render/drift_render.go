package main

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

// The drift audit's "intent": which interfaces each SR Linux node must
// have admin-state enable on (every link member, backbone and cabinet
// alike), and which IS-IS subinterfaces must carry no metric override
// (backbone members + loopback; cabinet-link ports run BGP, not IS-IS).
// Derived from spec links — the same facts srl_render.go programs.

type driftIface struct {
	LinkID string `json:"link_id"`
}

type driftNode struct {
	Interfaces map[string]driftIface `json:"interfaces"`
	ISIS       []string              `json:"isis_interfaces"`
}

func driftExpected(s *Spec) map[string]driftNode {
	srl := map[string]bool{}
	for _, n := range s.Nodes {
		if n.Kind == KindSRLinux {
			srl[n.Name] = true
		}
	}
	out := map[string]driftNode{}
	get := func(name string) driftNode {
		if d, ok := out[name]; ok {
			return d
		}
		return driftNode{Interfaces: map[string]driftIface{}, ISIS: []string{"lo0.0"}}
	}
	for _, l := range s.Links {
		for _, ep := range []Endpoint{l.A, l.B} {
			if !srl[ep.Node] {
				continue
			}
			d := get(ep.Node)
			d.Interfaces[ep.Intf] = driftIface{LinkID: l.ID}
			if l.Kind == LinkKindBackbone {
				d.ISIS = append(d.ISIS, ep.Intf+".0")
			}
			out[ep.Node] = d
		}
	}
	for name, d := range out {
		sort.Strings(d.ISIS)
		out[name] = d
	}
	return out
}

// WriteDriftExpected emits the audited-intent JSON consumed by
// drift_compare.py (mounted via the workflow-scripts ConfigMap). JSON
// cannot carry the render banner; render-check still diff-gates it.
func WriteDriftExpected(w io.Writer, s *Spec) error {
	b, err := json.MarshalIndent(driftExpected(s), "", "  ")
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "%s\n", b)
	return err
}

// WriteWFTDriftAudit emits the drift-audit WorkflowTemplate: one gnmic
// config-fetch task per SR Linux node feeding a single compare step.
// Built programmatically (not a .tmpl) because the task list is
// spec-derived.
func WriteWFTDriftAudit(w io.Writer, s *Spec) error {
	var nodes []string
	for _, n := range s.Nodes {
		if n.Kind == KindSRLinux {
			nodes = append(nodes, n.Name)
		}
	}
	sort.Strings(nodes)

	envSafe := func(n string) string {
		return strings.ToUpper(strings.ReplaceAll(n, "-", "_"))
	}

	var b strings.Builder
	fmt.Fprintf(&b, "%s\n", renderBanner)
	b.WriteString(`apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: drift-audit
spec:
  serviceAccountName: operate-workflow-sa
  entrypoint: audit
  volumes:
    - name: scripts
      configMap:
        name: workflow-scripts
  templates:
    - name: audit
      dag:
        tasks:
`)
	for _, n := range nodes {
		fmt.Fprintf(&b, `          - name: get-%s
            template: get-config
            arguments:
              parameters:
                - {name: node, value: "%s"}
`, n, n)
	}
	b.WriteString(`          - name: compare
            template: compare
            depends: "`)
	deps := make([]string, len(nodes))
	for i, n := range nodes {
		deps[i] = "get-" + n
	}
	b.WriteString(strings.Join(deps, " && "))
	b.WriteString("\"\n            arguments:\n              parameters:\n")
	for _, n := range nodes {
		fmt.Fprintf(&b, "                - {name: live_%s, value: \"{{tasks.get-%s.outputs.result}}\"}\n",
			strings.ToLower(envSafe(n)), n)
	}

	// get-config: /app/gnmic (binary is NOT on the image PATH). The
	// `|| echo '[]'` keeps an unreachable node from failing the DAG;
	// the compare step reports it and audits the rest.
	fmt.Fprintf(&b, `
    - name: get-config
      inputs:
        parameters:
          - name: node
      script:
        image: %s
        command: [sh]
        source: |
          /app/gnmic -a %s-{{inputs.parameters.node}}.clabernetes.svc.cluster.local:57400 \
            -u admin -p 'NokiaSrl1!' --skip-verify -e json_ietf \
            get --type config \
            --path "/interface" \
            --path "/network-instance[name=default]/protocols/srl_nokia-isis:isis/instance[name=%s]" \
            2>/dev/null || echo '[]'
        resources:
          requests: {cpu: 30m, memory: 32Mi}
          limits:   {memory: 128Mi}

    - name: compare
      inputs:
        parameters:
`, ImageGNMIC, s.Metadata.Name, ISISInstance)
	for _, n := range nodes {
		fmt.Fprintf(&b, "          - name: live_%s\n", strings.ToLower(envSafe(n)))
	}
	fmt.Fprintf(&b, `      script:
        image: %s
        command: [python3]
        source: |
          import sys, runpy
          sys.path.insert(0, "/scripts")
          runpy.run_path("/scripts/drift_compare.py", run_name="__main__")
        volumeMounts:
          - name: scripts
            mountPath: /scripts
        env:
          - name: EXPECTED_PATH
            value: /scripts/drift_expected.json
          - name: ALERTMANAGER_URL
            value: http://kps-kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093
          - name: VALKEY_URL
            value: valkey://valkey.valkey.svc.cluster.local:6379/2
`, ImageEventingPy)
	for _, n := range nodes {
		fmt.Fprintf(&b, "          - name: LIVE_%s\n            value: \"{{inputs.parameters.live_%s}}\"\n",
			envSafe(n), strings.ToLower(envSafe(n)))
	}
	b.WriteString(`        resources:
          requests: {cpu: 50m, memory: 96Mi}
          limits:   {memory: 192Mi}
`)
	_, err := io.WriteString(w, b.String())
	return err
}
