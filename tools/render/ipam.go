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

// ISISNet builds an SR Linux NET (Network Entity Title) for a node loopback.
// Layout: <area>.0000.0000.<last_octet_4d>.00 — area is the AFI+area prefix
// (e.g. "49.0001"), NSEL=00, system-id derived from the loopback's last octet
// as 4 zero-padded decimal digits. Unique within our /24 loopback range.
func ISISNet(area, loopback string) string {
	return fmt.Sprintf("%s.0000.0000.%04d.00", area, LastOctet(loopback))
}
