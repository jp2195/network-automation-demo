package main

import "testing"

// validSpec returns a minimal spec that passes Validate, for tests to mutate.
func validSpec() *Spec {
	return &Spec{
		Agencies: []Agency{
			{Slug: "adot-region-7", Name: "Atlas DOT Region 7"},
		},
		Nodes: []Node{
			{Name: "hub-a", Kind: KindSRLinux, Role: RoleCorridorHub, LoopbackV4: "10.0.0.1", ISISSID: 16001},
			{Name: "hub-b", Kind: KindSRLinux, Role: RoleTMC, LoopbackV4: "10.0.0.2", ISISSID: 16002},
			{Name: "fc-x", Kind: KindFRR, Role: RoleFieldCabinet, LoopbackV4: "10.0.0.3", ParentHub: "hub-a",
				Agencies: []string{"adot-region-7"}},
		},
		Links: []Link{
			{ID: "l1", Kind: LinkKindBackbone, A: Endpoint{"hub-a", "ethernet-1/1"}, B: Endpoint{"hub-b", "ethernet-1/1"}, SubnetV4: "10.1.1.0/30"},
			{ID: "l2", Kind: LinkKindCabinet, A: Endpoint{"hub-a", "ethernet-1/2"}, B: Endpoint{"fc-x", "eth1"}, SubnetV4: "10.2.1.0/30"},
		},
	}
}

func TestValidateValid(t *testing.T) {
	if err := validSpec().Validate(); err != nil {
		t.Fatalf("valid spec rejected: %v", err)
	}
}

func TestValidateRejects(t *testing.T) {
	cases := map[string]func(*Spec){
		"duplicate node":      func(s *Spec) { s.Nodes[1].Name = "hub-a" },
		"undeclared endpoint": func(s *Spec) { s.Links[0].A.Node = "ghost" },
		"bad subnet":          func(s *Spec) { s.Links[0].SubnetV4 = "not-a-cidr" },
		"duplicate loopback":  func(s *Spec) { s.Nodes[1].LoopbackV4 = "10.0.0.1" },
		"duplicate isis_sid":  func(s *Spec) { s.Nodes[1].ISISSID = 16001 },
		"duplicate interface": func(s *Spec) { s.Links[1].A = Endpoint{"hub-a", "ethernet-1/1"} },
		"missing parent_hub":  func(s *Spec) { s.Nodes[2].ParentHub = "nope" },

		"parent_hub not a corridor-hub": func(s *Spec) { s.Nodes[2].ParentHub = "hub-b" },
		"unknown agency slug":           func(s *Spec) { s.Nodes[2].Agencies = []string{"ghost-agency"} },
		"unknown node kind":             func(s *Spec) { s.Nodes[0].Kind = "junos" },
		"unknown link kind":             func(s *Spec) { s.Links[0].Kind = "wireless" },
		"srl interface naming":          func(s *Spec) { s.Links[0].A.Intf = "eth1" },
		"frr interface naming":          func(s *Spec) { s.Links[1].B.Intf = "ethernet-1/9" },
		"backbone link to a cabinet":    func(s *Spec) { s.Links[0].B = Endpoint{"fc-x", "eth2"} },
		"cabinet link without cabinet":  func(s *Spec) { s.Links[1].B = Endpoint{"hub-b", "ethernet-1/2"} },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			s := validSpec()
			mutate(s)
			if err := s.Validate(); err == nil {
				t.Errorf("%s: expected validation error, got nil", name)
			}
		})
	}
}
