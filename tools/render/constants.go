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
	ImageFRR     = "atlas-demo-registry:5001/frr-snmpd:latest"
	ImagePython  = "python:3.12-slim"
	ImageGNMIC   = "ghcr.io/openconfig/gnmic:0.44.1"
)
