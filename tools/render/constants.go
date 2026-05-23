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
)
