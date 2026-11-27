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
	// SR Linux held on 25.3.3: every 26.x image crashloops on Apple
	// Silicon (net_inst_mgr "File exists" creating the mgmt gateway
	// veth), see issue #38. renovate.json holds it below 26 to match.
	ImageSRLinux = "ghcr.io/nokia/srlinux:25.3.3"
	// FRR stays on the public image (snmpd installed at boot by
	// wrapper.sh). Before Clabernetes 0.9 the nested docker couldn't pull
	// from atlas-demo-registry:5001; kubelet pulls now, so the pre-baked
	// images/frr-snmpd could replace it (needs the registry trusted as
	// plain HTTP in globalConfig.imagePull) — not switched yet.
	ImageFRR    = "quay.io/frrouting/frr:10.7.1"
	ImagePython = "python:3.15-slim"
	ImageGNMIC  = "ghcr.io/openconfig/gnmic:0.49.1"
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

	// Named operator accounts, so a change is attributed to WHO made it
	// (in the AAA syslog → Loki) instead of the generic admin/root. The
	// manual demo-cut authenticates as NocOps; the closed-loop remediation
	// lane authenticates as Automation — so the audit trail tells a human
	// change apart from an automated one by user + source. Demo passwords,
	// not real credentials (same status as GNMICPassword; see SECRETS.md).
	NocOpsUser         = "noc-ops"
	NocOpsPassword     = "NocOps1!"
	AutomationUser     = "svc-automation"
	AutomationPassword = "SvcAuto1!"

	// ClabDomain is the in-cluster DNS suffix clabernetes exposes each
	// topology node Service under (<node>.<ClabDomain>). Mirrored as
	// CLAB_DOMAIN in workloads/eventing/scripts/constants.py.
	ClabDomain = "clabernetes.svc.cluster.local"

	AlloySyslogHost = "alloy-syslog.monitoring.svc.cluster.local"
	AlloySyslogPort = "5514"
)
