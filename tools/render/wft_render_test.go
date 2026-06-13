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
		"WriteWFTAIAnalyst":         WriteWFTAIAnalyst,
		"WriteWFTGrayFailure":       WriteWFTGrayFailure,
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
		if !strings.Contains(out, ImageEventingPy) && (name == "WriteWFTEnrichedNotify" || name == "WriteWFTIncidentCollector" || name == "WriteWFTRemediation" || name == "WriteWFTDriftAudit" || name == "WriteWFTGrayFailure") {
			t.Errorf("%s: eventing image %q missing from output", name, ImageEventingPy)
		}
		if !strings.Contains(out, ImageGNMIC) && (name == "WriteWFTRemediation" || name == "WriteWFTDriftAudit") {
			t.Errorf("%s: gnmic image %q missing from output", name, ImageGNMIC)
		}
		if name == "WriteWFTAIAnalyst" {
			if !strings.Contains(out, ImageAIAnalyst) {
				t.Errorf("%s: ai-analyst image %q missing from output", name, ImageAIAnalyst)
			}
			// Positive value checks: a replacer-key/template-token rename
			// would otherwise emit empty env values without tripping the
			// sentinel guard above.
			if !strings.Contains(out, `value: "`+s.Metadata.Name+`"`) {
				t.Errorf("%s: CLAB_PREFIX value %q missing from output", name, s.Metadata.Name)
			}
			if !strings.Contains(out, `value: "`+ISISInstance+`"`) {
				t.Errorf("%s: ISIS_INSTANCE value %q missing from output", name, ISISInstance)
			}
		}
	}
}
