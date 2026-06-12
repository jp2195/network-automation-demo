package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func driftTestSpec() *Spec {
	s := &Spec{}
	s.Metadata.Name = "atlanta"
	s.Nodes = []Node{
		{Name: "hub-e", Kind: KindSRLinux},
		{Name: "hub-i20e", Kind: KindSRLinux},
		{Name: "fc-i20e", Kind: KindFRR},
	}
	s.Links = []Link{
		{ID: "ring-e-i20e", Kind: LinkKindBackbone,
			A: Endpoint{Node: "hub-e", Intf: "ethernet-1/1"},
			B: Endpoint{Node: "hub-i20e", Intf: "ethernet-1/2"}},
		{ID: "hubi20e-fci20e", Kind: LinkKindCabinet,
			A: Endpoint{Node: "hub-i20e", Intf: "ethernet-1/4"},
			B: Endpoint{Node: "fc-i20e", Intf: "eth1"}},
	}
	return s
}

func TestDriftExpectedDerivation(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteDriftExpected(&buf, driftTestSpec()); err != nil {
		t.Fatal(err)
	}
	var got map[string]struct {
		Interfaces map[string]struct {
			LinkID string `json:"link_id"`
		} `json:"interfaces"`
		ISIS []string `json:"isis_interfaces"`
	}
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("emitted JSON invalid: %v", err)
	}
	if _, ok := got["fc-i20e"]; ok {
		t.Error("FRR node must not be audited")
	}
	he := got["hub-e"]
	if he.Interfaces["ethernet-1/1"].LinkID != "ring-e-i20e" {
		t.Errorf("hub-e ethernet-1/1 link_id = %q", he.Interfaces["ethernet-1/1"].LinkID)
	}
	hi := got["hub-i20e"]
	// Cabinet-link interface audited for admin-state...
	if hi.Interfaces["ethernet-1/4"].LinkID != "hubi20e-fci20e" {
		t.Errorf("hub-i20e ethernet-1/4 link_id = %q", hi.Interfaces["ethernet-1/4"].LinkID)
	}
	// ...but NOT in IS-IS (BGP runs to the cabinet).
	for _, sub := range hi.ISIS {
		if sub == "ethernet-1/4.0" {
			t.Error("cabinet-link interface must not be expected in IS-IS")
		}
	}
	// Backbone subif + loopback are.
	want := map[string]bool{"ethernet-1/2.0": false, "lo0.0": false}
	for _, sub := range hi.ISIS {
		if _, ok := want[sub]; ok {
			want[sub] = true
		}
	}
	for sub, seen := range want {
		if !seen {
			t.Errorf("hub-i20e missing expected IS-IS interface %s", sub)
		}
	}
}

func TestWFTDriftAuditShape(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteWFTDriftAudit(&buf, driftTestSpec()); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	for _, want := range []string{
		"name: drift-audit",
		"- name: get-hub-e",
		"- name: get-hub-i20e",
		"/app/gnmic",
		"LIVE_HUB_E",
		"LIVE_HUB_I20E",
		"{{tasks.get-hub-e.outputs.result}}",
		ImageGNMIC,
		ImageEventingPy,
		"atlanta-{{inputs.parameters.node}}.clabernetes.svc.cluster.local:57400",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("WFT output missing %q", want)
		}
	}
	if strings.Contains(out, "get-fc-i20e") {
		t.Error("FRR node must not get an audit task")
	}
}
