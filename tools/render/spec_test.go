package main

import "testing"

// validSpec returns a minimal spec that passes Validate, for tests to mutate.
func validSpec() *Spec {
	return &Spec{
		Nodes: []Node{
			{Name: "hub-a", Kind: KindSRLinux, LoopbackV4: "10.0.0.1", ISISSID: 16001},
			{Name: "hub-b", Kind: KindSRLinux, LoopbackV4: "10.0.0.2", ISISSID: 16002},
			{Name: "fc-x", Kind: KindFRR, LoopbackV4: "10.0.0.3", ParentHub: "hub-a"},
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
