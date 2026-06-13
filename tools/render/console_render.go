package main

import (
	"encoding/json"
	"io"
	"sort"
)

type consoleNode struct {
	Name       string   `json:"name"`
	Kind       string   `json:"kind"`
	Role       string   `json:"role"`
	Interfaces []string `json:"interfaces"`
}

type consoleLink struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	A    string `json:"a"`
	B    string `json:"b"`
}

type consoleTargets struct {
	Nodes []consoleNode `json:"nodes"`
	Links []consoleLink `json:"links"`
}

// WriteConsoleTargets renders the node/interface/link picker data for the
// scenario console from spec/atlanta.yaml — single source of truth so the
// UI never drifts. Emitted into tools/console/static/ for //go:embed.
func WriteConsoleTargets(w io.Writer, s *Spec) error {
	ifaces := map[string]map[string]bool{}
	add := func(node, intf string) {
		if node == "" {
			return
		}
		if ifaces[node] == nil {
			ifaces[node] = map[string]bool{}
		}
		if intf != "" {
			ifaces[node][intf] = true
		}
	}
	links := make([]consoleLink, 0, len(s.Links))
	for _, l := range s.Links {
		add(l.A.Node, l.A.Intf)
		add(l.B.Node, l.B.Intf)
		links = append(links, consoleLink{
			ID: l.ID, Kind: l.Kind,
			A: l.A.Node + ":" + l.A.Intf,
			B: l.B.Node + ":" + l.B.Intf,
		})
	}
	nodes := make([]consoleNode, 0, len(s.Nodes))
	for _, n := range s.Nodes {
		list := make([]string, 0, len(ifaces[n.Name]))
		for i := range ifaces[n.Name] {
			list = append(list, i)
		}
		sort.Strings(list)
		nodes = append(nodes, consoleNode{
			Name: n.Name, Kind: n.Kind, Role: n.Role, Interfaces: list,
		})
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(consoleTargets{Nodes: nodes, Links: links})
}
