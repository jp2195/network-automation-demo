package main

import (
	"bytes"
	"io"
	"strings"
	"testing"
)

// TestWFTOutputsHaveNoSentinels guards the @@...@@ substitution contract:
// a renamed constant or template token must not ship an unreplaced
// sentinel inside a WorkflowTemplate.
func TestWFTOutputsHaveNoSentinels(t *testing.T) {
	s := &Spec{}
	s.Metadata.Name = "atlas-demo"

	writers := map[string]func(io.Writer, *Spec) error{
		"WriteWFTCutFiber":          WriteWFTCutFiber,
		"WriteWFTIncidentCollector": WriteWFTIncidentCollector,
		"WriteWFTEnrichedNotify":    WriteWFTEnrichedNotify,
		"WriteWFTMaintenance":       WriteWFTMaintenance,
		"WriteWFTRemediation":       WriteWFTRemediation,
		"WriteWFTDriftAudit":        WriteWFTDriftAudit,
	}
	for name, fn := range writers {
		var buf bytes.Buffer
		if err := fn(&buf, s); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		out := buf.String()
		if strings.Contains(out, "@@") {
			i := strings.Index(out, "@@")
			t.Errorf("%s: unreplaced sentinel near %q", name, out[i:min(i+30, len(out))])
		}
		if !strings.Contains(out, ImageEventingPy) && (name == "WriteWFTEnrichedNotify" || name == "WriteWFTIncidentCollector" || name == "WriteWFTRemediation" || name == "WriteWFTDriftAudit") {
			t.Errorf("%s: eventing image %q missing from output", name, ImageEventingPy)
		}
		if !strings.Contains(out, ImageGNMIC) && (name == "WriteWFTRemediation" || name == "WriteWFTDriftAudit") {
			t.Errorf("%s: gnmic image %q missing from output", name, ImageGNMIC)
		}
	}
}
