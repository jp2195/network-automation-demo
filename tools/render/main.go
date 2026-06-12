package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
)

func main() {
	var (
		specPath = flag.String("spec", "spec/atlanta.yaml", "path to topology spec")
		outDir   = flag.String("out", ".", "output root (workloads/* paths land under this)")
	)
	flag.Parse()

	fmt.Printf("==> Loading %s\n", *specPath)
	spec, err := LoadSpec(*specPath)
	if err != nil {
		log.Fatal(err)
	}

	var srl, frr int
	for _, n := range spec.Nodes {
		switch n.Kind {
		case KindSRLinux:
			srl++
		case KindFRR:
			frr++
		}
	}
	fmt.Printf("    %d nodes (%d srlinux, %d frr), %d links, %d agencies\n",
		len(spec.Nodes), srl, frr, len(spec.Links), len(spec.Agencies))

	cfgDir := filepath.Join(*outDir, "workloads", "topology", "startup-configs")
	gnmicDir := filepath.Join(*outDir, "workloads", "gnmic")
	netboxDir := filepath.Join(*outDir, "workloads", "netbox")
	netboxSeedDir := filepath.Join(netboxDir, "seed")
	topoDir := filepath.Join(*outDir, "workloads", "topology")
	obsDir := filepath.Join(*outDir, "workloads", "observability")
	domDir := filepath.Join(*outDir, "workloads", "dom-synth")
	for _, d := range []string{cfgDir, gnmicDir, netboxDir, netboxSeedDir, topoDir, obsDir, domDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			log.Fatal(err)
		}
	}

	fmt.Printf("==> Rendering SR Linux startup configs → %s/\n", cfgDir)
	var srlNames []string
	for i := range spec.Nodes {
		n := &spec.Nodes[i]
		if n.Kind != KindSRLinux {
			continue
		}
		renderTo(filepath.Join(cfgDir, n.Name+".cfg"), "",
			func(w io.Writer) error { return WriteSRL(w, n, spec) })
		srlNames = append(srlNames, n.Name+".cfg")
	}
	fmt.Printf("    %s\n", strings.Join(srlNames, " "))

	fmt.Printf("==> Rendering FRR configs → %s/\n", cfgDir)
	var frrNames []string
	for i := range spec.Nodes {
		n := &spec.Nodes[i]
		if n.Kind != KindFRR {
			continue
		}
		renderTo(filepath.Join(cfgDir, n.Name+".frr"), "",
			func(w io.Writer) error { return WriteFRR(w, n, spec) })
		frrNames = append(frrNames, n.Name+".frr")
	}
	fmt.Printf("    %s\n", strings.Join(frrNames, " "))

	renderTo(filepath.Join(gnmicDir, "targets.yaml"), fmt.Sprintf(" (%d targets)", srl),
		func(w io.Writer) error { return WriteGNMIC(w, spec) })
	renderTo(filepath.Join(netboxSeedDir, "seed.json"),
		fmt.Sprintf(" (%d devices, %d cables, %d tenants)", len(spec.Nodes), len(spec.Links), len(spec.Agencies)),
		func(w io.Writer) error { return WriteNetBox(w, spec) })
	renderTo(filepath.Join(topoDir, "topology.yaml"),
		fmt.Sprintf(" (clabernetes Topology, %d nodes, %d links)", len(spec.Nodes), len(spec.Links)),
		func(w io.Writer) error { return WriteTopology(w, spec) })
	renderTo(filepath.Join(topoDir, "kustomization.yaml"), "",
		func(w io.Writer) error { return WriteTopologyKustomization(w, spec) })
	renderTo(filepath.Join(cfgDir, "daemons"), " (FRR daemons)",
		func(w io.Writer) error { return WriteFRRDaemons(w) })
	renderTo(filepath.Join(cfgDir, "snmpd.conf"), " (FRR cabinet snmpd.conf)",
		func(w io.Writer) error { return WriteSNMPDConf(w) })
	renderTo(filepath.Join(cfgDir, "wrapper.sh"), " (FRR cabinet entrypoint wrapper)",
		func(w io.Writer) error { return WriteFRRWrapper(w) })
	renderTo(filepath.Join(obsDir, "link-membership.yaml"),
		fmt.Sprintf(" (%d link endpoints)", len(spec.Links)*2),
		func(w io.Writer) error { return WriteLinkMembership(w, spec) })
	renderTo(filepath.Join(obsDir, "link-rate-rules.yaml"), "",
		func(w io.Writer) error { return WriteLinkRateRules(w, spec) })
	renderTo(filepath.Join(domDir, "links.json"), " (DOM synth interface list)",
		func(w io.Writer) error { return WriteDOMLinks(w, spec) })

	snmpDir := filepath.Join(*outDir, "workloads", "snmp")
	if err := os.MkdirAll(snmpDir, 0o755); err != nil {
		log.Fatal(err)
	}
	renderTo(filepath.Join(snmpDir, "probe.yaml"),
		fmt.Sprintf(" (%d FRR cabinet probe targets)", len(FRRSNMPTargets(spec))),
		func(w io.Writer) error { return WriteSNMPProbe(w, spec) })

	eventingDir := filepath.Join(*outDir, "workloads", "eventing")
	for _, d := range []string{eventingDir, filepath.Join(eventingDir, "scripts")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			log.Fatal(err)
		}
	}
	renderTo(filepath.Join(eventingDir, "wft-cut-fiber.yaml"), "",
		func(w io.Writer) error { return WriteWFTCutFiber(w, spec) })
	renderTo(filepath.Join(eventingDir, "wft-incident-collector.yaml"), "",
		func(w io.Writer) error { return WriteWFTIncidentCollector(w, spec) })
	renderTo(filepath.Join(eventingDir, "wft-enriched-notify.yaml"), "",
		func(w io.Writer) error { return WriteWFTEnrichedNotify(w, spec) })
	renderTo(filepath.Join(eventingDir, "wft-maintenance.yaml"), "",
		func(w io.Writer) error { return WriteWFTMaintenance(w, spec) })
	renderTo(filepath.Join(eventingDir, "wft-remediation.yaml"), "",
		func(w io.Writer) error { return WriteWFTRemediation(w, spec) })
	renderTo(filepath.Join(eventingDir, "wft-drift-audit.yaml"), "",
		func(w io.Writer) error { return WriteWFTDriftAudit(w, spec) })
	renderTo(filepath.Join(eventingDir, "scripts", "drift_expected.json"), "",
		func(w io.Writer) error { return WriteDriftExpected(w, spec) })

	renderTo(filepath.Join(*outDir, "workloads", "versions.yaml"), "",
		func(w io.Writer) error { return WriteVersions(w) })

	dashboardsDir := filepath.Join(*outDir, "workloads", "observability", "dashboards")
	renderTo(filepath.Join(dashboardsDir, "geomap.json"), "",
		func(w io.Writer) error { return WriteGeomap(w, spec) })

	fmt.Printf("==> Rewriting dashboard cross-nav in %s/\n", dashboardsDir)
	if err := RewriteDashboards(dashboardsDir); err != nil {
		log.Fatal(err)
	}

	fmt.Println("==> Done.")
}
