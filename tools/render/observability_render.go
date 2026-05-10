package main

import (
	"fmt"
	"io"
)

// WriteLinkMembership emits a PrometheusRule with two recording rules:
//
//   - link_membership_info{node, interface, link_id, link_kind} — lifts link
//     identity onto interface-level series for alerts/dashboards.
//   - device_geo_info{node, kind, role, site, city, lat, lon} — pins every
//     spec node to its geographic coords so Grafana's geomap can place
//     devices without external geojson hosting.
//   - link_geo_segment{link_id, link_kind, lat_a, lon_a, lat_b, lon_b,
//     node_a, node_b, cable_label} — one row per backbone/cabinet link with
//     both endpoints' coordinates so Grafana can draw cables on the map.
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
			p("            cable_label: %q", l.Cable.Label)
			if l.Cable.Corridor != "" {
				p("            corridor: %q", l.Cable.Corridor)
			}
			if l.Cable.Provider != "" {
				p("            provider: %q", l.Cable.Provider)
			}
			if l.Cable.RestorationSLAHours != 0 {
				p("            restoration_sla_hours: %q", fmt.Sprintf("%d", l.Cable.RestorationSLAHours))
			}
		}
	}
	for _, n := range s.Nodes {
		p("        - record: device_geo_info")
		p("          expr: vector(1)")
		p("          labels:")
		p("            node: %q", n.Name)
		p("            kind: %q", n.Kind)
		p("            role: %q", n.Role)
		p("            site: %q", n.Site.Label)
		p("            city: %q", n.Site.City)
		p("            lat: %q", fmt.Sprintf("%.4f", n.Site.Lat))
		p("            lon: %q", fmt.Sprintf("%.4f", n.Site.Lon))
	}
	for _, l := range s.Links {
		na := s.NodeByName(l.A.Node)
		nb := s.NodeByName(l.B.Node)
		if na == nil || nb == nil {
			continue
		}
		p("        - record: link_geo_segment")
		p("          expr: vector(1)")
		p("          labels:")
		p("            link_id: %q", l.ID)
		p("            link_kind: %q", l.Kind)
		p("            cable_label: %q", l.Cable.Label)
		p("            node_a: %q", l.A.Node)
		p("            node_b: %q", l.B.Node)
		p("            lat_a: %q", fmt.Sprintf("%.4f", na.Site.Lat))
		p("            lon_a: %q", fmt.Sprintf("%.4f", na.Site.Lon))
		p("            lat_b: %q", fmt.Sprintf("%.4f", nb.Site.Lat))
		p("            lon_b: %q", fmt.Sprintf("%.4f", nb.Site.Lon))
		// Two point rows per link so the geomap "route" layer can connect
		// them as a line. Sequence label preserves a-then-b ordering.
		for i, ep := range []struct {
			n   *Node
			seq string
		}{{na, "1"}, {nb, "2"}} {
			_ = i
			p("        - record: link_endpoint_geo")
			p("          expr: vector(1)")
			p("          labels:")
			p("            link_id: %q", l.ID)
			p("            link_kind: %q", l.Kind)
			p("            cable_label: %q", l.Cable.Label)
			p("            seq: %q", ep.seq)
			p("            node: %q", ep.n.Name)
			p("            lat: %q", fmt.Sprintf("%.4f", ep.n.Site.Lat))
			p("            lon: %q", fmt.Sprintf("%.4f", ep.n.Site.Lon))
		}
	}
	return nil
}
