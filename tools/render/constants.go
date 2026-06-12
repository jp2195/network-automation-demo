package main

// Mirrors workloads/eventing/scripts/constants.py. Keep in sync.

const (
	KindSRLinux = "srlinux"
	KindFRR     = "frr"

	RoleTMC          = "tmc"
	RoleCorridorHub  = "corridor-hub"
	RoleFieldCabinet = "field-cabinet"

	LinkKindBackbone = "backbone"
	LinkKindCabinet  = "cabinet"

	// Severity classes — mirror constants.py (match the emoji map in notify.py).
	SeverityHigh    = "high"
	SeverityMedium  = "medium"
	SeverityWarning = "warning"
	SeverityLow     = "low"

	// CabinetNamePrefix identifies field-cabinet device names (e.g. fc-i20e).
	CabinetNamePrefix = "fc-"
)

// Image versions used across the demo workloads. Mirrored to
// workloads/versions.yaml so dom-synth, eventing, and topology can
// all reference one source.
const (
	ImageSRLinux = "ghcr.io/nokia/srlinux:25.3.3"
	// FRR stays on the public image — clabernetes' containerlab can't
	// resolve atlas-demo-registry:5001/... refs from inside the nested
	// docker (it prepends `docker.io/` and the ref becomes invalid).
	// The pre-baked frr-snmpd Dockerfile + `make build` slot remain in
	// place for a future imagePullThrough fix.
	ImageFRR    = "quay.io/frrouting/frr:10.6.1"
	ImagePython = "python:3.12-slim"
	ImageGNMIC  = "ghcr.io/openconfig/gnmic:0.44.1"
	// ImageEventingPy is the pre-baked eventing image (python + slack-sdk +
	// valkey + scripts) pushed to the in-cluster registry. Single source for
	// the incident-collector bundle step and the enriched-notify WFT.
	ImageEventingPy = "atlas-demo-registry:5001/eventing-py:latest"
	// ImageAIAnalyst is the pre-baked advisory-lane image (python +
	// pydantic-ai + read-only tool deps: pygnmi Get, puresnmp, slack-sdk).
	// Used only by the ai-analyst WFT; the deterministic pipeline never
	// touches it.
	ImageAIAnalyst = "atlas-demo-registry:5001/ai-analyst:latest"
)

// ISISInstance is the IS-IS instance name programmed into every SR Linux
// node (srl_render.go) and addressed by the remediation WorkflowTemplate's
// gNMI metric path (wft_render.go). Single-sourced here so the two can
// never drift.
const ISISInstance = "atlas"

// Shared infra endpoints / credentials used across emitters (Go-only; not
// mirrored in constants.py). The demo gNMI password is a documented default
// (see SECRETS.md), not a real credential.
const (
	GNMICUser     = "admin"
	GNMICPassword = "NokiaSrl1!"
	GNMICPort     = "57400"

	// ClabDomain is the in-cluster DNS suffix clabernetes exposes each
	// topology node Service under (<topology>-<node>.<ClabDomain>).
	ClabDomain = "clabernetes.svc.cluster.local"

	AlloySyslogHost = "alloy-syslog.monitoring.svc.cluster.local"
	AlloySyslogPort = "5514"
)
