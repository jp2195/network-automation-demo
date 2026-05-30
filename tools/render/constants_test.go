package main

import (
	"os"
	"regexp"
	"testing"
)

// TestConstantsMirrorPython guards the "Keep in sync" contract between
// tools/render/constants.go and workloads/eventing/scripts/constants.py.
// It parses the Python file and asserts every shared constant has the same
// value as its Go counterpart, so drift in either direction fails the build.
func TestConstantsMirrorPython(t *testing.T) {
	// pythonName -> expected value (from the Go constants).
	want := map[string]string{
		"KIND_SRLINUX":        KindSRLinux,
		"KIND_FRR":            KindFRR,
		"ROLE_TMC":            RoleTMC,
		"ROLE_CORRIDOR_HUB":   RoleCorridorHub,
		"ROLE_FIELD_CABINET":  RoleFieldCabinet,
		"LINK_KIND_BACKBONE":  LinkKindBackbone,
		"LINK_KIND_CABINET":   LinkKindCabinet,
		"SEVERITY_HIGH":       SeverityHigh,
		"SEVERITY_MEDIUM":     SeverityMedium,
		"SEVERITY_WARNING":    SeverityWarning,
		"SEVERITY_LOW":        SeverityLow,
		"CABINET_NAME_PREFIX": CabinetNamePrefix,
	}

	src, err := os.ReadFile("../../workloads/eventing/scripts/constants.py")
	if err != nil {
		t.Fatalf("read constants.py: %v", err)
	}
	// Match: NAME = "value"  (ignoring surrounding whitespace).
	re := regexp.MustCompile(`(?m)^([A-Z_]+)\s*=\s*"([^"]*)"`)
	got := map[string]string{}
	for _, m := range re.FindAllStringSubmatch(string(src), -1) {
		got[m[1]] = m[2]
	}

	for name, wantVal := range want {
		gotVal, ok := got[name]
		if !ok {
			t.Errorf("constants.py missing %s (Go has it = %q)", name, wantVal)
			continue
		}
		if gotVal != wantVal {
			t.Errorf("%s drift: constants.py=%q, constants.go=%q", name, gotVal, wantVal)
		}
	}
}
