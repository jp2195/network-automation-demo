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
	geojsonDir := filepath.Join(*outDir, "workloads", "observability", "dashboards")
	for _, d := range []string{cfgDir, gnmicDir, netboxDir, geojsonDir} {
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

	seedPath := filepath.Join(netboxDir, "seed.json")
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
