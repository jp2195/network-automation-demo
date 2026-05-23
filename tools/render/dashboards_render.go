package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// dashboardLink is one entry in a Grafana dashboard's top-level `links`
// array. Grafana renders these as a dropdown menu in the top-right corner.
type dashboardLink struct {
	Title string `json:"title"`
	Type  string `json:"type"`
	URL   string `json:"url"`
	Icon  string `json:"icon"`
}

// canonicalNav returns the canonical cross-nav for every dashboard,
// minus the dashboard whose own uid matches `currentUID`.
func canonicalNav(currentUID string) []dashboardLink {
	all := []struct{ title, url, uid string }{
		{"Overview", "/d/network-overview/network-overview", "network-overview"},
		{"Geomap", "/d/geomap/atlanta-metro-geomap", "geomap"},
		{"Topology", "/d/topology-graph/topology-timeline", "topology-graph"},
		{"Corridor status", "/d/corridor-status/corridor-status", "corridor-status"},
		{"Link detail", "/d/link-detail/link-detail", "link-detail"},
		{"Device detail", "/d/device-detail/device-detail", "device-detail"},
		{"Cabinet detail", "/d/cabinet-detail/cabinet-detail", "cabinet-detail"},
		{"Alert console", "/d/alert-console/alert-console", "alert-console"},
		{"Audit feed", "/d/audit-feed/audit-feed", "audit-feed"},
		{"Hardware health", "/d/hardware-health/hardware-health", "hardware-health"},
		{"Capacity", "/d/capacity-planning/capacity-planning", "capacity-planning"},
		{"Route flaps", "/d/route-flaps/route-flaps", "route-flaps"},
		{"Routing", "/d/routing-protocols/routing-protocols", "routing-protocols"},
	}
	out := make([]dashboardLink, 0, len(all))
	for _, e := range all {
		if e.uid == currentUID {
			continue
		}
		out = append(out, dashboardLink{
			Title: e.title,
			Type:  "link",
			URL:   e.url,
			Icon:  "external link",
		})
	}
	return out
}

// RewriteDashboards walks the dashboard directory, parses each JSON,
// replaces its `links` array with the canonical nav (excluding self),
// and writes back. Other keys are preserved (encoding/json's MarshalIndent
// over a map[string]interface{} writes keys in sorted order — Grafana
// accepts any key order so this is fine).
func RewriteDashboards(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		// Dashboards are hand-authored and only present in the source tree;
		// when render-check renders into a scratch directory, the dashboards
		// dir won't exist — that's fine, nothing to rewrite.
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".json" {
			continue
		}
		path := filepath.Join(dir, e.Name())
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var doc map[string]interface{}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber() // preserve numeric literals (Grafana cares about ints vs floats)
		if err := dec.Decode(&doc); err != nil {
			return fmt.Errorf("parse %s: %w", path, err)
		}
		uid, _ := doc["uid"].(string)
		nav := canonicalNav(uid)
		// Marshal back to []interface{} so the surrounding doc shape stays generic.
		links := make([]interface{}, len(nav))
		for i, l := range nav {
			links[i] = map[string]interface{}{
				"title": l.Title,
				"type":  l.Type,
				"url":   l.URL,
				"icon":  l.Icon,
			}
		}
		doc["links"] = links

		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetIndent("", "  ")
		enc.SetEscapeHTML(false) // preserve `&`, `<`, `>` literally — Grafana JSON does not HTML-escape
		if err := enc.Encode(doc); err != nil {
			return err
		}
		if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
			return err
		}
	}
	return nil
}
