package main

import (
	"fmt"
	"net"
)

// IPPair returns the (.1, .2) hosts of a /30.
func IPPair(cidr string) (a, b string) {
	ip, _, err := net.ParseCIDR(cidr)
	if err != nil {
		return "", ""
	}
	ip4 := ip.To4()
	if ip4 == nil {
		return "", ""
	}
	a = net.IPv4(ip4[0], ip4[1], ip4[2], ip4[3]+1).String()
	b = net.IPv4(ip4[0], ip4[1], ip4[2], ip4[3]+2).String()
	return
}

// LastOctet returns the last octet of an IPv4 address.
func LastOctet(ipStr string) int {
	ip := net.ParseIP(ipStr).To4()
	if ip == nil {
		return 0
	}
	return int(ip[3])
}

// ISISSystemID derives the IS-IS system-id (0000.0000.<last_octet_4d>) from a
// node loopback. This is the single source of truth for the system-id — both
// the real SR Linux NET (ISISNet) and the synthetic DOM-synth adjacency rows
// must use it so they agree. Unique within our /24 loopback range.
func ISISSystemID(loopback string) string {
	return fmt.Sprintf("0000.0000.%04d", LastOctet(loopback))
}

// ISISNet builds an SR Linux NET (Network Entity Title) for a node loopback.
// Layout: <area>.<system-id>.00 — area is the AFI+area prefix (e.g. "49.0001"),
// NSEL=00, system-id from ISISSystemID.
func ISISNet(area, loopback string) string {
	return fmt.Sprintf("%s.%s.00", area, ISISSystemID(loopback))
}
