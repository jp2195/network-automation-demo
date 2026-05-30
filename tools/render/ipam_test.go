package main

import (
	"strings"
	"testing"
)

func TestLastOctet(t *testing.T) {
	cases := map[string]int{
		"10.0.0.13":  13,
		"10.0.0.1":   1,
		"10.0.0.255": 255,
		"not-an-ip":  0,
	}
	for in, want := range cases {
		if got := LastOctet(in); got != want {
			t.Errorf("LastOctet(%q) = %d, want %d", in, got, want)
		}
	}
}

func TestIPPair(t *testing.T) {
	a, b := IPPair("10.1.1.0/30")
	if a != "10.1.1.1" || b != "10.1.1.2" {
		t.Errorf("IPPair(10.1.1.0/30) = %q, %q; want 10.1.1.1, 10.1.1.2", a, b)
	}
}

// The DOM-synth synthetic neighbor system-id MUST equal the system-id the
// real SR Linux NET embeds, or the isis_adjacencies rows reference a
// neighbor no device advertises. Both must derive from the loopback's last
// octet — NOT from the ISIS SID (which overflows %04d and differs from the
// loopback octet).
func TestISISSystemIDMatchesNET(t *testing.T) {
	const loop = "10.0.0.13"
	sid := ISISSystemID(loop)
	if sid != "0000.0000.0013" {
		t.Errorf("ISISSystemID(%q) = %q, want 0000.0000.0013", loop, sid)
	}
	net := ISISNet("49.0001", loop)
	if want := "49.0001.0000.0000.0013.00"; net != want {
		t.Errorf("ISISNet = %q, want %q", net, want)
	}
	if !strings.Contains(net, sid) {
		t.Errorf("NET %q does not embed system-id %q", net, sid)
	}
}
