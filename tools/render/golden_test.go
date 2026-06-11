package main

import (
	"bytes"
	"flag"
	"io"
	"os"
	"path/filepath"
	"testing"
)

var update = flag.Bool("update", false, "rewrite golden files under testdata/")

// goldenSpec decorates validSpec with the descriptive fields the NetBox and
// Topology emitters consume. It is deliberately NOT spec/atlanta.yaml —
// drift in the committed outputs is `make render-check`'s job; these
// goldens pin emitter behavior against a small stable fixture instead.
func goldenSpec(t *testing.T) *Spec {
	t.Helper()
	s := validSpec()
	s.Metadata.Name = "golden-demo"
	s.Providers = []Provider{{Slug: "ringlight-fiber", Name: "Ringlight Fiber"}}
	s.Nodes[0].Site = Site{Label: "hub-a-site", City: "Goldenville", Corridor: "I-00", Lat: 33.7, Lon: -84.4, District: 7}
	s.Nodes[1].Site = Site{Label: "hub-b-site", City: "Specton", Lat: 33.8, Lon: -84.3, District: 7}
	s.Nodes[2].Site = Site{Label: "fc-x-site", City: "Fixture Falls", Corridor: "I-00", Lat: 33.9, Lon: -84.2, District: 8}
	s.Nodes[2].ASN = 64512
	s.Nodes[2].ITSInventory = ITSInventory{CCTV: 2, SignalControllers: 1, DMS: 1, RampMeters: 1}
	s.Links[1].Cable = Cable{
		Label: "OSP-GOLD-001", RouteDescription: "I-00 trench", LengthKm: 1.2,
		Installed: "2020-01-01", Provider: "ringlight-fiber",
		CircuitID: "RF-0001", RestorationSLAHours: 8,
	}
	if err := s.Validate(); err != nil {
		t.Fatalf("golden fixture invalid: %v", err)
	}
	return s
}

func TestGoldenOutputs(t *testing.T) {
	s := goldenSpec(t)
	cases := map[string]func(io.Writer, *Spec) error{
		"netbox-seed.json": WriteNetBox,
		"topology.yaml":    WriteTopology,
	}
	for name, fn := range cases {
		t.Run(name, func(t *testing.T) {
			var buf bytes.Buffer
			if err := fn(&buf, s); err != nil {
				t.Fatalf("render: %v", err)
			}
			golden := filepath.Join("testdata", name)
			if *update {
				if err := os.MkdirAll("testdata", 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(golden, buf.Bytes(), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(golden)
			if err != nil {
				t.Fatalf("read golden (run `go test -run TestGoldenOutputs -update` to create): %v", err)
			}
			if !bytes.Equal(buf.Bytes(), want) {
				t.Errorf("%s drifted from golden — if intentional, re-run with -update and review the diff", name)
			}
		})
	}
}
