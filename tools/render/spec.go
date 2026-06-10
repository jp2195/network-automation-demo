package main

import (
	"fmt"
	"net"
	"os"
	"regexp"

	"gopkg.in/yaml.v3"
)

// Interface naming is load-bearing: srl_render/frr_render/clabIntf all
// assume SR Linux ports are "ethernet-1/N" and FRR cabinets "ethN".
var (
	srlIntfRe = regexp.MustCompile(`^ethernet-1/\d+$`)
	frrIntfRe = regexp.MustCompile(`^eth\d+$`)
)

type Spec struct {
	Metadata  Metadata   `yaml:"metadata"`
	DOT       DOT        `yaml:"dot"`
	Agencies  []Agency   `yaml:"agencies"`
	Providers []Provider `yaml:"providers"`
	Nodes     []Node     `yaml:"nodes"`
	Links     []Link     `yaml:"links"`

	// Indexed views of Nodes / Links, populated by LoadSpec. Use these
	// in hot paths instead of NodeByName / InterfacesOf, which are O(N).
	nodesByName      map[string]*Node
	interfacesByNode map[string][]IfaceOnNode
}

type Metadata struct {
	Name        string `yaml:"name"`
	Description string `yaml:"description"`
	ISIS        struct {
		Area  string `yaml:"area"`
		Level string `yaml:"level"`
	} `yaml:"isis"`
	ASN struct {
		Backbone int `yaml:"backbone"`
	} `yaml:"asn"`
	IPAM struct {
		LoopbackV4 string `yaml:"loopback_v4"`
		BackboneV4 string `yaml:"backbone_v4"`
		CabinetV4  string `yaml:"cabinet_v4"`
	} `yaml:"ipam"`
}

type DOT struct {
	Slug   string `yaml:"slug"`
	Name   string `yaml:"name"`
	Region string `yaml:"region"`
	OnCall struct {
		Primary   string `yaml:"primary"`
		Secondary string `yaml:"secondary"`
	} `yaml:"on_call"`
}

type Agency struct {
	Slug string `yaml:"slug"`
	Name string `yaml:"name"`
}

type Provider struct {
	Slug string `yaml:"slug"`
	Name string `yaml:"name"`
}

type Site struct {
	Label    string  `yaml:"label"`
	City     string  `yaml:"city"`
	Corridor string  `yaml:"corridor"`
	Lat      float64 `yaml:"lat"`
	Lon      float64 `yaml:"lon"`
	District int     `yaml:"district"`
}

type ITSInventory struct {
	CCTV              int `yaml:"cctv"`
	SignalControllers int `yaml:"signal_controllers"`
	DMS               int `yaml:"dms"`
	RampMeters        int `yaml:"ramp_meters"`
}

type Node struct {
	Name         string       `yaml:"name"`
	Kind         string       `yaml:"kind"`
	Role         string       `yaml:"role"`
	Description  string       `yaml:"description"`
	Site         Site         `yaml:"site"`
	LoopbackV4   string       `yaml:"loopback_v4"`
	ISISSID      int          `yaml:"isis_sid"`
	ASN          int          `yaml:"asn"`
	ParentHub    string       `yaml:"parent_hub"`
	ITSInventory ITSInventory `yaml:"its_inventory"`
	Agencies     []string     `yaml:"agencies"`
}

type Endpoint struct {
	Node string `yaml:"node"`
	Intf string `yaml:"intf"`
}

type Cable struct {
	Label               string  `yaml:"label"`
	Corridor            string  `yaml:"corridor"`
	RouteDescription    string  `yaml:"route_description"`
	LengthKm            float64 `yaml:"length_km"`
	Installed           string  `yaml:"installed"`
	Provider            string  `yaml:"provider"`
	CircuitID           string  `yaml:"circuit_id"`
	RestorationSLAHours int     `yaml:"restoration_sla_hours"`
}

type Link struct {
	ID       string   `yaml:"id"`
	Kind     string   `yaml:"kind"`
	A        Endpoint `yaml:"a"`
	B        Endpoint `yaml:"b"`
	SubnetV4 string   `yaml:"subnet_v4"`
	Cable    Cable    `yaml:"cable"`
}

func LoadSpec(path string) (*Spec, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var s Spec
	if err := yaml.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	s.nodesByName = make(map[string]*Node, len(s.Nodes))
	for i := range s.Nodes {
		s.nodesByName[s.Nodes[i].Name] = &s.Nodes[i]
	}
	s.interfacesByNode = make(map[string][]IfaceOnNode, len(s.Nodes))
	for i := range s.Nodes {
		s.interfacesByNode[s.Nodes[i].Name] = computeInterfaces(&s, s.Nodes[i].Name)
	}
	return &s, nil
}

func (s *Spec) Validate() error {
	agencySlugs := map[string]bool{}
	for _, a := range s.Agencies {
		agencySlugs[a.Slug] = true
	}
	seen := map[string]bool{}
	byName := map[string]Node{}
	loopbacks := map[string]string{} // loopback_v4 -> node
	sids := map[int]string{}         // isis_sid -> node
	for _, n := range s.Nodes {
		if seen[n.Name] {
			return fmt.Errorf("duplicate node %q", n.Name)
		}
		seen[n.Name] = true
		byName[n.Name] = n
		if n.Kind != KindSRLinux && n.Kind != KindFRR {
			return fmt.Errorf("node %q: unknown kind %q", n.Name, n.Kind)
		}
		for _, ag := range n.Agencies {
			if !agencySlugs[ag] {
				return fmt.Errorf("node %q: agency %q not declared in agencies", n.Name, ag)
			}
		}
		if n.LoopbackV4 != "" {
			if other, ok := loopbacks[n.LoopbackV4]; ok {
				return fmt.Errorf("duplicate loopback_v4 %s on %q and %q", n.LoopbackV4, other, n.Name)
			}
			loopbacks[n.LoopbackV4] = n.Name
		}
		if n.ISISSID != 0 {
			if other, ok := sids[n.ISISSID]; ok {
				return fmt.Errorf("duplicate isis_sid %d on %q and %q", n.ISISSID, other, n.Name)
			}
			sids[n.ISISSID] = n.Name
		}
	}
	// parent_hub references resolve against the full node set (second pass so
	// a cabinet declared before its hub still validates).
	for _, n := range s.Nodes {
		if n.ParentHub == "" {
			continue
		}
		hub, ok := byName[n.ParentHub]
		if !ok {
			return fmt.Errorf("node %q: parent_hub %q not declared", n.Name, n.ParentHub)
		}
		if hub.Role != RoleCorridorHub {
			return fmt.Errorf("node %q: parent_hub %q has role %q, want %q", n.Name, n.ParentHub, hub.Role, RoleCorridorHub)
		}
	}
	ifaces := map[string]string{} // "node|intf" -> link id
	for _, l := range s.Links {
		if !seen[l.A.Node] {
			return fmt.Errorf("link %s: side a node %q not declared", l.ID, l.A.Node)
		}
		if !seen[l.B.Node] {
			return fmt.Errorf("link %s: side b node %q not declared", l.ID, l.B.Node)
		}
		if _, _, err := net.ParseCIDR(l.SubnetV4); err != nil {
			return fmt.Errorf("link %s: bad subnet %q: %w", l.ID, l.SubnetV4, err)
		}
		// Link shape: backbones join two SR Linux nodes; cabinet links join
		// one SR Linux hub and one FRR cabinet. dom_render/frr_render bake
		// these assumptions in.
		ak, bk := byName[l.A.Node].Kind, byName[l.B.Node].Kind
		switch l.Kind {
		case LinkKindBackbone:
			if ak != KindSRLinux || bk != KindSRLinux {
				return fmt.Errorf("link %s: backbone links must join two %s nodes (got %s/%s)", l.ID, KindSRLinux, ak, bk)
			}
		case LinkKindCabinet:
			if !(ak == KindSRLinux && bk == KindFRR) && !(ak == KindFRR && bk == KindSRLinux) {
				return fmt.Errorf("link %s: cabinet links must join one %s and one %s node (got %s/%s)", l.ID, KindSRLinux, KindFRR, ak, bk)
			}
		default:
			return fmt.Errorf("link %s: unknown kind %q", l.ID, l.Kind)
		}
		for _, ep := range []Endpoint{l.A, l.B} {
			key := ep.Node + "|" + ep.Intf
			if other, ok := ifaces[key]; ok {
				return fmt.Errorf("link %s: interface %s/%s already used by link %s", l.ID, ep.Node, ep.Intf, other)
			}
			ifaces[key] = l.ID
			switch byName[ep.Node].Kind {
			case KindSRLinux:
				if !srlIntfRe.MatchString(ep.Intf) {
					return fmt.Errorf("link %s: %s interface %q on %s must match ethernet-1/N", l.ID, KindSRLinux, ep.Intf, ep.Node)
				}
			case KindFRR:
				if !frrIntfRe.MatchString(ep.Intf) {
					return fmt.Errorf("link %s: %s interface %q on %s must match ethN", l.ID, KindFRR, ep.Intf, ep.Node)
				}
			}
		}
	}
	return nil
}

func (s *Spec) NodeByName(name string) *Node {
	if s.nodesByName != nil {
		return s.nodesByName[name]
	}
	for i := range s.Nodes {
		if s.Nodes[i].Name == name {
			return &s.Nodes[i]
		}
	}
	return nil
}

// IfaceOnNode is one local interface viewpoint of a link.
type IfaceOnNode struct {
	LinkID   string
	LinkKind string
	Name     string
	LocalV4  string
	PeerNode string
	PeerIntf string
	PeerV4   string
	Cable    Cable
	Subnet   string
}

func (s *Spec) InterfacesOf(nodeName string) []IfaceOnNode {
	if s.interfacesByNode != nil {
		return s.interfacesByNode[nodeName]
	}
	return computeInterfaces(s, nodeName)
}

func computeInterfaces(s *Spec, nodeName string) []IfaceOnNode {
	var out []IfaceOnNode
	for _, l := range s.Links {
		var local, peer Endpoint
		var localFirst bool
		switch nodeName {
		case l.A.Node:
			local, peer, localFirst = l.A, l.B, true
		case l.B.Node:
			local, peer, localFirst = l.B, l.A, false
		default:
			continue
		}
		a, b := IPPair(l.SubnetV4)
		var localIP, peerIP string
		if localFirst {
			localIP, peerIP = a, b
		} else {
			localIP, peerIP = b, a
		}
		out = append(out, IfaceOnNode{
			LinkID:   l.ID,
			LinkKind: l.Kind,
			Name:     local.Intf,
			LocalV4:  localIP,
			PeerNode: peer.Node,
			PeerIntf: peer.Intf,
			PeerV4:   peerIP,
			Cable:    l.Cable,
			Subnet:   l.SubnetV4,
		})
	}
	return out
}
