package main

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
)

// geomapBase is the static geomap shell (basemap, controls, view, fieldConfig,
// transformations, tags …) with empty layers/targets/links. WriteGeomap fills
// the spec-derived node + per-link layers; RewriteDashboards fills cross-nav.
//
//go:embed templates/geomap.base.json
var geomapBase string

// Grafana 13's geomap can't bind a query value field to marker/line color
// (renders white), so status is encoded by a query-split with fixed colors:
// an "up" query and a "down" query per node-class / per link, each layer a
// fixed green/red, shown only when its query returns rows. See the geomap
// commit message and docs for why field-color is avoided.
const (
	geoCabUp = `(max by (node, lat, lon, site, city, role, kind) (device_geo_info{kind="frr"} * on(node) group_right(lat, lon, site, city, role, kind) (2 - up{job="snmp-frr-cabinets"}))) == 1`
	geoCabDn = `(max by (node, lat, lon, site, city, role, kind) (device_geo_info{kind="frr"} * on(node) group_right(lat, lon, site, city, role, kind) (2 - up{job="snmp-frr-cabinets"}))) >= 2`
	geoHubUp = `(max by (node, lat, lon, site, city, role, kind) (device_geo_info{kind="srlinux"} * on(node) group_right(lat, lon, site, city, role, kind) (srl_nokia_interfaces_interface_oper_state * on(node, interface) group_left link_membership_info))) == 1`
	geoHubDn = `(max by (node, lat, lon, site, city, role, kind) (device_geo_info{kind="srlinux"} * on(node) group_right(lat, lon, site, city, role, kind) (srl_nokia_interfaces_interface_oper_state * on(node, interface) group_left link_membership_info))) >= 2`
)

func geoTarget(refID, expr string) map[string]interface{} {
	return map[string]interface{}{
		"refId":   refID,
		"format":  "table",
		"instant": true,
		"expr":    expr,
	}
}

func geoRouteLayer(refID, color string) map[string]interface{} {
	return map[string]interface{}{
		"type":       "route",
		"name":       refID,
		"tooltip":    false,
		"filterData": map[string]interface{}{"id": "byRefId", "options": refID},
		"location":   map[string]interface{}{"mode": "coords", "latitude": "lat", "longitude": "lon"},
		"config": map[string]interface{}{
			"showLegend": false,
			"arrow":      0,
			"style": map[string]interface{}{
				"color":     map[string]interface{}{"fixed": color},
				"lineWidth": 3,
				"opacity":   0.85,
			},
		},
	}
}

func geoMarkerLayer(name, refID, color, symbol string, size int) map[string]interface{} {
	return map[string]interface{}{
		"type":       "markers",
		"name":       name,
		"tooltip":    true,
		"filterData": map[string]interface{}{"id": "byRefId", "options": refID},
		"location":   map[string]interface{}{"mode": "coords", "latitude": "lat", "longitude": "lon"},
		"config": map[string]interface{}{
			"showLegend": true,
			"style": map[string]interface{}{
				"color":   map[string]interface{}{"fixed": color},
				"opacity": 0.95,
				"size":    map[string]interface{}{"fixed": size},
				"symbol":  map[string]interface{}{"mode": "fixed", "fixed": "img/icons/marker/" + symbol + ".svg"},
				"text":    map[string]interface{}{"field": "node"},
				"textConfig": map[string]interface{}{
					"fontSize":     12,
					"offsetY":      -16,
					"textAlign":    "center",
					"textBaseline": "bottom",
				},
			},
		},
	}
}

// WriteGeomap emits the geomap dashboard JSON: per-link line layers (green up /
// red down) under per-node-class up/down marker layers, all spec-derived.
func WriteGeomap(w io.Writer, s *Spec) error {
	dec := json.NewDecoder(bytes.NewReader([]byte(geomapBase)))
	dec.UseNumber()
	var doc map[string]interface{}
	if err := dec.Decode(&doc); err != nil {
		return fmt.Errorf("parse geomap base: %w", err)
	}

	targets := []interface{}{}
	layers := []interface{}{} // links on the bottom, nodes on top
	for i, l := range s.Links {
		up, dn := fmt.Sprintf("U%d", i), fmt.Sprintf("D%d", i)
		targets = append(targets,
			geoTarget(up, fmt.Sprintf(`link_endpoint_geo{link_id="%s"} and on(link_id) (max by (link_id) (link:oper_state_with_meta) == 1)`, l.ID)),
			geoTarget(dn, fmt.Sprintf(`link_endpoint_geo{link_id="%s"} and on(link_id) (max by (link_id) (link:oper_state_with_meta) == 2)`, l.ID)),
		)
		layers = append(layers, geoRouteLayer(up, "green"), geoRouteLayer(dn, "red"))
	}
	targets = append(targets,
		geoTarget("Aup", geoCabUp), geoTarget("Adn", geoCabDn),
		geoTarget("Bup", geoHubUp), geoTarget("Bdn", geoHubDn),
	)
	layers = append(layers,
		geoMarkerLayer("Cabinets up", "Aup", "green", "triangle", 11),
		geoMarkerLayer("Cabinets DOWN", "Adn", "red", "triangle", 13),
		geoMarkerLayer("Hubs up", "Bup", "green", "circle", 15),
		geoMarkerLayer("Hubs DOWN", "Bdn", "red", "circle", 17),
	)

	panel := doc["panels"].([]interface{})[0].(map[string]interface{})
	panel["targets"] = targets
	panel["options"].(map[string]interface{})["layers"] = layers

	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	return enc.Encode(doc)
}
