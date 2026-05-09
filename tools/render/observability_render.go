package main

import (
	"fmt"
	"io"
)

// WriteLinkMembership emits a PrometheusRule that records one link_membership_info{...}
// timeseries per (node, interface, link_id, link_kind) tuple. The metric is used by
// Grafana / alert templates to enrich gNMIc metrics with link identity at query time
// without needing a join against NetBox.
func WriteLinkMembership(w io.Writer, s *Spec) error {
	p := func(format string, args ...interface{}) {
		fmt.Fprintf(w, format+"\n", args...)
	}
	p("apiVersion: monitoring.coreos.com/v1")
	p("kind: PrometheusRule")
	p("metadata:")
	p("  name: link-membership")
	p("  labels:")
	p("    app.kubernetes.io/name: link-membership")
	p("    release: kps")
	p("spec:")
	p("  groups:")
	p("    - name: link-membership")
	p("      interval: 60s")
	p("      rules:")
	for _, l := range s.Links {
		for _, ep := range []Endpoint{l.A, l.B} {
			p("        - record: link_membership_info")
			p("          expr: vector(1)")
			p("          labels:")
			p("            node: %q", ep.Node)
			p("            interface: %q", ep.Intf)
			p("            link_id: %q", l.ID)
			p("            link_kind: %q", l.Kind)
		}
	}
	return nil
}
