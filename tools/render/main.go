package main

import (
	"flag"
	"fmt"
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
		case "srlinux":
			srl++
		case "frr":
			frr++
		}
	}
	fmt.Printf("    %d nodes (%d srlinux, %d frr), %d links, %d agencies\n",
		len(spec.Nodes), srl, frr, len(spec.Links), len(spec.Agencies))

	cfgDir := filepath.Join(*outDir, "workloads", "topology", "startup-configs")
	gnmicDir := filepath.Join(*outDir, "workloads", "gnmic")
	netboxDir := filepath.Join(*outDir, "workloads", "netbox")
	netboxSeedDir := filepath.Join(netboxDir, "seed")
	geojsonDir := filepath.Join(*outDir, "workloads", "observability", "dashboards")
	for _, d := range []string{cfgDir, gnmicDir, netboxDir, netboxSeedDir, geojsonDir} {
		must(os.MkdirAll(d, 0o755))
	}

	fmt.Printf("==> Rendering SR Linux startup configs → %s/\n", cfgDir)
	var srlNames []string
	for _, n := range spec.Nodes {
		if n.Kind != "srlinux" {
			continue
		}
		path := filepath.Join(cfgDir, n.Name+".cfg")
		f := mustCreate(path)
		must(WriteSRL(f, &n, spec))
		f.Close()
		srlNames = append(srlNames, n.Name+".cfg")
	}
	fmt.Printf("    %s\n", strings.Join(srlNames, " "))

	fmt.Printf("==> Rendering FRR configs → %s/\n", cfgDir)
	var frrNames []string
	for _, n := range spec.Nodes {
		if n.Kind != "frr" {
			continue
		}
		path := filepath.Join(cfgDir, n.Name+".frr")
		f := mustCreate(path)
		must(WriteFRR(f, &n, spec))
		f.Close()
		frrNames = append(frrNames, n.Name+".frr")
	}
	fmt.Printf("    %s\n", strings.Join(frrNames, " "))

	targetsPath := filepath.Join(gnmicDir, "targets.yaml")
	fmt.Printf("==> Writing %s (%d targets)\n", targetsPath, srl)
	f := mustCreate(targetsPath)
	must(WriteGNMIC(f, spec))
	f.Close()

	seedPath := filepath.Join(netboxSeedDir, "seed.json")
	fmt.Printf("==> Writing %s (%d devices, %d cables, %d tenants)\n",
		seedPath, len(spec.Nodes), len(spec.Links), len(spec.Agencies))
	f = mustCreate(seedPath)
	must(WriteNetBox(f, spec))
	f.Close()

	geoPath := filepath.Join(geojsonDir, "links.geojson")
	fmt.Printf("==> Writing %s (%d features)\n", geoPath, len(spec.Links))
	f = mustCreate(geoPath)
	must(WriteGeoJSON(f, spec))
	f.Close()

	topoDir := filepath.Join(*outDir, "workloads", "topology")
	must(os.MkdirAll(topoDir, 0o755))

	topoPath := filepath.Join(topoDir, "topology.yaml")
	fmt.Printf("==> Writing %s (clabernetes Topology, %d nodes, %d links)\n",
		topoPath, len(spec.Nodes), len(spec.Links))
	f = mustCreate(topoPath)
	must(WriteTopology(f, spec))
	f.Close()

	kustPath := filepath.Join(topoDir, "kustomization.yaml")
	fmt.Printf("==> Writing %s\n", kustPath)
	f = mustCreate(kustPath)
	must(WriteTopologyKustomization(f, spec))
	f.Close()

	daemonsPath := filepath.Join(cfgDir, "daemons")
	fmt.Printf("==> Writing %s (FRR daemons)\n", daemonsPath)
	f = mustCreate(daemonsPath)
	must(WriteFRRDaemons(f))
	f.Close()

	snmpdConfPath := filepath.Join(cfgDir, "snmpd.conf")
	fmt.Printf("==> Writing %s (FRR cabinet snmpd.conf)\n", snmpdConfPath)
	f = mustCreate(snmpdConfPath)
	must(WriteSNMPDConf(f))
	f.Close()

	wrapperPath := filepath.Join(cfgDir, "wrapper.sh")
	fmt.Printf("==> Writing %s (FRR cabinet entrypoint wrapper)\n", wrapperPath)
	f = mustCreate(wrapperPath)
	must(WriteFRRWrapper(f))
	f.Close()

	obsDir := filepath.Join(*outDir, "workloads", "observability")
	must(os.MkdirAll(obsDir, 0o755))

	lmPath := filepath.Join(obsDir, "link-membership.yaml")
	fmt.Printf("==> Writing %s (%d link endpoints)\n", lmPath, len(spec.Links)*2)
	f = mustCreate(lmPath)
	must(WriteLinkMembership(f, spec))
	f.Close()

	fmt.Println("==> Done.")
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func mustCreate(path string) *os.File {
	f, err := os.Create(path)
	must(err)
	return f
}
