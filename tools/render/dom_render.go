package main

import (
	"encoding/json"
	"io"
)

// WriteDOMLinks emits a JSON array describing every backbone+cabinet
// interface endpoint in the spec. The dom-synth exporter reads this file
// and emits one synthetic transceiver metric per row.
func WriteDOMLinks(w io.Writer, s *Spec) error {
	type row struct {
		Node      string `json:"node"`
		Interface string `json:"interface"`
		LinkID    string `json:"link_id"`
		LinkKind  string `json:"link_kind"`
	}
	rows := make([]row, 0, len(s.Links)*2)
	for _, l := range s.Links {
		for _, ep := range []Endpoint{l.A, l.B} {
			n := s.NodeByName(ep.Node)
			if n == nil || n.Kind != "srlinux" {
				// only SR Linux ends have transceivers we'd care to model
				continue
			}
			rows = append(rows, row{
				Node:      ep.Node,
				Interface: ep.Intf,
				LinkID:    l.ID,
				LinkKind:  l.Kind,
			})
		}
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(rows)
}
