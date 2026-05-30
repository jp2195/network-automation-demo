package main

import (
	"encoding/json"
	"io"
)

// WriteDOMLinks emits a JSON file describing the per-port + per-node
// state the synthetic equipment exporter should emit metrics for. It
// covers three things:
//
//   - "ports": one row per backbone + cabinet endpoint on an SR Linux
//     node. Drives transceiver DOM metrics (temperature, Rx/Tx power,
//     bias current, voltage).
//   - "nodes": one row per spec node. Drives chassis temperature, fan
//     RPM, and PSU voltage metrics. Counts depend on chassis profile.
//   - "isis_adjacencies": one row per backbone link endpoint pair. Each
//     row represents an expected IS-IS adjacency between two nodes,
//     identified by (interface, neighbor system-id).
//   - "bgp_peers": one row per cabinet uplink. Each row represents an
//     expected eBGP session between a corridor-hub and a cabinet.
func WriteDOMLinks(w io.Writer, s *Spec) error {
	type port struct {
		Node      string `json:"node"`
		Interface string `json:"interface"`
		LinkID    string `json:"link_id"`
		LinkKind  string `json:"link_kind"`
	}
	type node struct {
		Name    string `json:"node"`
		Kind    string `json:"kind"`
		Role    string `json:"role"`
		Site    string `json:"site"`
		Chassis string `json:"chassis"`
		FanIDs  []string `json:"fan_ids"`
		PSUIDs  []string `json:"psu_ids"`
		TempIDs []string `json:"temp_ids"`
	}
	type isisAdj struct {
		Node      string `json:"node"`
		Interface string `json:"interface"`
		Neighbor  string `json:"neighbor"`
		SystemID  string `json:"system_id"`
		LinkID    string `json:"link_id"`
		Level     string `json:"level"`
	}
	type bgpPeer struct {
		Node     string `json:"node"`
		Neighbor string `json:"neighbor_node"`
		Address  string `json:"peer_address"`
		PeerAS   int    `json:"peer_as"`
		Group    string `json:"peer_group"`
	}

	out := struct {
		Ports     []port    `json:"ports"`
		Nodes     []node    `json:"nodes"`
		ISIS      []isisAdj `json:"isis_adjacencies"`
		BGPPeers  []bgpPeer `json:"bgp_peers"`
	}{}

	for _, l := range s.Links {
		for _, ep := range []Endpoint{l.A, l.B} {
			n := s.NodeByName(ep.Node)
			if n == nil || n.Kind != KindSRLinux {
				continue
			}
			out.Ports = append(out.Ports, port{
				Node:      ep.Node,
				Interface: ep.Intf,
				LinkID:    l.ID,
				LinkKind:  l.Kind,
			})
		}
	}

	// Per-node hardware: SR Linux ixr-d3 has 2 PSU, 4 fans, several temp
	// sensors. FRR cabinets are linux containers — model 1 PSU, 1 fan,
	// 1 chassis temp.
	for _, n := range s.Nodes {
		entry := node{
			Name: n.Name,
			Kind: n.Kind,
			Role: n.Role,
			Site: n.Site.Label,
		}
		switch n.Kind {
		case KindSRLinux:
			entry.Chassis = "7220 IXR-D3"
			entry.FanIDs = []string{"fan1", "fan2", "fan3", "fan4"}
			entry.PSUIDs = []string{"psu1", "psu2"}
			entry.TempIDs = []string{"intake", "exhaust", "linecard", "cpu"}
		case KindFRR:
			entry.Chassis = "ATSP-FC-1U"
			entry.FanIDs = []string{"fan1"}
			entry.PSUIDs = []string{"psu1"}
			entry.TempIDs = []string{"intake", "cpu"}
		}
		out.Nodes = append(out.Nodes, entry)
	}

	// IS-IS adjacencies: one per *each end* of every backbone link, so
	// a link between A and B yields two rows (A's view of B, B's view
	// of A). Cabinet links use eBGP, not IS-IS.
	for _, l := range s.Links {
		if l.Kind != LinkKindBackbone {
			continue
		}
		na := s.NodeByName(l.A.Node)
		nb := s.NodeByName(l.B.Node)
		if na == nil || nb == nil {
			continue
		}
		out.ISIS = append(out.ISIS, isisAdj{
			Node:      l.A.Node,
			Interface: l.A.Intf,
			Neighbor:  l.B.Node,
			SystemID:  ISISSystemID(nb.LoopbackV4),
			LinkID:    l.ID,
			Level:     "L2",
		})
		out.ISIS = append(out.ISIS, isisAdj{
			Node:      l.B.Node,
			Interface: l.B.Intf,
			Neighbor:  l.A.Node,
			SystemID:  ISISSystemID(na.LoopbackV4),
			LinkID:    l.ID,
			Level:     "L2",
		})
	}

	// BGP peers: each cabinet link is a corridor-hub → cabinet eBGP
	// session. Backbone iBGP between TMCs is also represented.
	for _, l := range s.Links {
		if l.Kind != LinkKindCabinet {
			continue
		}
		na := s.NodeByName(l.A.Node)
		nb := s.NodeByName(l.B.Node)
		if na == nil || nb == nil {
			continue
		}
		// hub-to-cabinet from the hub's perspective
		hubSide := l.A
		hubNode, cabNode := na, nb
		if cabNode.Kind == KindSRLinux {
			hubSide = l.B
			hubNode, cabNode = nb, na
		}
		// ifc lookup gives us the /30 the spec assigned.
		var hubIfc *IfaceOnNode
		for _, ifc := range s.InterfacesOf(hubNode.Name) {
			if ifc.Name == hubSide.Intf {
				ic := ifc
				hubIfc = &ic
				break
			}
		}
		peerAddr := ""
		if hubIfc != nil {
			peerAddr = hubIfc.PeerV4
		}
		out.BGPPeers = append(out.BGPPeers, bgpPeer{
			Node:     hubNode.Name,
			Neighbor: cabNode.Name,
			Address:  peerAddr,
			PeerAS:   cabNode.ASN,
			Group:    "cabinets",
		})
		// view from cabinet side too — useful for the dashboard
		out.BGPPeers = append(out.BGPPeers, bgpPeer{
			Node:     cabNode.Name,
			Neighbor: hubNode.Name,
			Address:  hubAddrV4(hubIfc),
			PeerAS:   s.Metadata.ASN.Backbone,
			Group:    "uplink",
		})
	}
	// TMC iBGP mesh: each TMC peers with every other TMC over loopbacks.
	tmcs := []*Node{}
	for i := range s.Nodes {
		if s.Nodes[i].Role == RoleTMC {
			tmcs = append(tmcs, &s.Nodes[i])
		}
	}
	for _, a := range tmcs {
		for _, b := range tmcs {
			if a == b {
				continue
			}
			out.BGPPeers = append(out.BGPPeers, bgpPeer{
				Node:     a.Name,
				Neighbor: b.Name,
				Address:  b.LoopbackV4,
				PeerAS:   s.Metadata.ASN.Backbone,
				Group:    "tmc-ibgp",
			})
		}
	}

	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

func hubAddrV4(ifc *IfaceOnNode) string {
	if ifc == nil {
		return ""
	}
	return ifc.LocalV4
}
