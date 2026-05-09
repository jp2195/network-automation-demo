package main

import (
	"encoding/json"
	"io"
)

type feature struct {
	Type       string                 `json:"type"`
	Properties map[string]interface{} `json:"properties"`
	Geometry   geometry               `json:"geometry"`
}

type geometry struct {
	Type        string      `json:"type"`
	Coordinates [][]float64 `json:"coordinates"`
}

type featureCollection struct {
	Type     string    `json:"type"`
	Features []feature `json:"features"`
}

func WriteGeoJSON(w io.Writer, s *Spec) error {
	fc := featureCollection{Type: "FeatureCollection"}
	for _, l := range s.Links {
		a := s.NodeByName(l.A.Node)
		b := s.NodeByName(l.B.Node)
		if a == nil || b == nil {
			continue
		}
		fc.Features = append(fc.Features, feature{
			Type: "Feature",
			Properties: map[string]interface{}{
				"link_id":           l.ID,
				"kind":              l.Kind,
				"cable_label":       l.Cable.Label,
				"corridor":          l.Cable.Corridor,
				"route_description": l.Cable.RouteDescription,
			},
			Geometry: geometry{
				Type: "LineString",
				Coordinates: [][]float64{
					{a.Site.Lon, a.Site.Lat},
					{b.Site.Lon, b.Site.Lat},
				},
			},
		})
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(fc)
}
