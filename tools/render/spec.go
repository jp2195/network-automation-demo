package main

import (
	"fmt"
	"net"
	"os"

	"gopkg.in/yaml.v3"
)

type Spec struct {
	Metadata  Metadata   `yaml:"metadata"`
	DOT       DOT        `yaml:"dot"`
	Agencies  []Agency   `yaml:"agencies"`
	Providers []Provider `yaml:"providers"`
	Nodes     []Node     `yaml:"nodes"`
	Links     []Link     `yaml:"links"`
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
	return &s, nil
}

func (s *Spec) Validate() error {
	seen := map[string]bool{}
	for _, n := range s.Nodes {
		if seen[n.Name] {
			return fmt.Errorf("duplicate node %q", n.Name)
		}
		seen[n.Name] = true
	}
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
	}
	return nil
}

func (s *Spec) NodeByName(name string) *Node {
	for i := range s.Nodes {
		if s.Nodes[i].Name == name {
			return &s.Nodes[i]
		}
	}
	return nil
}

func (s *Spec) AgencyBySlug(slug string) *Agency {
	for i := range s.Agencies {
		if s.Agencies[i].Slug == slug {
			return &s.Agencies[i]
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
