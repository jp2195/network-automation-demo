package main

import (
	"fmt"
	"io"
	"sort"
	"strings"
)

const (
	srlImage = "ghcr.io/nokia/srlinux:25.3.3"
	frrImage = "quay.io/frrouting/frr:10.6.1"
)

// frrDaemons is the standard FRR daemons file with zebra+bgpd enabled.
// Cabinets run eBGP toward their parent hub; no other daemons needed.
const frrDaemons = `zebra=yes
bgpd=yes
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
pathd=no

vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
bgpd_options="   -A 127.0.0.1"

# Run daemons as root so they keep CAP_NET_ADMIN/CAP_SYS_ADMIN. The
# upstream frrouting/frr image ships binaries without file capabilities,
# so dropping to the 'frr' user strips the caps the routing daemons need
# (privs_init in mgmtd/zebra/bgpd fails with EPERM otherwise).
frr_user="root"
frr_group="root"
`

// clabIntf translates a spec interface name to its containerlab-flavored form.
// SR Linux uses "ethernet-1/N" internally but containerlab expects "e1-N".
// Linux kinds (FRR cabinets) keep their native "ethN" form.
func clabIntf(kind, intf string) string {
	if kind == "srlinux" && strings.HasPrefix(intf, "ethernet-") {
		// ethernet-1/3 -> e1-3 (containerlab expects hyphens, not slashes)
		short := strings.TrimPrefix(intf, "ethernet-")
		return "e" + strings.ReplaceAll(short, "/", "-")
	}
	return intf
}

// configFilename returns the renderer-emitted startup-config filename for a node.
func configFilename(n Node) string {
	if n.Kind == "frr" {
		return n.Name + ".frr"
	}
	return n.Name + ".cfg"
}

// WriteContainerlab writes the containerlab topology YAML for the spec, suitable
// for embedding in a Topology CR's spec.definition.containerlab field.
func WriteContainerlab(w io.Writer, spec *Spec) error {
	fmt.Fprintf(w, "name: %s\n", spec.Metadata.Name)
	fmt.Fprintln(w, "topology:")
	fmt.Fprintln(w, "  kinds:")
	fmt.Fprintln(w, "    nokia_srlinux:")
	fmt.Fprintf(w, "      image: %s\n", srlImage)
	// FRR cabinets share daemons, snmpd.conf, and wrapper.sh; only the
	// per-node frr.conf is unique. Containerlab merges per-node binds with
	// kind-level binds, so the shared mounts live here.
	//
	// FRR 10.6's mgmtd/zebra/bgpd require CAP_NET_ADMIN + CAP_SYS_ADMIN, which
	// containerlab's `linux` kind doesn't grant by default. Without these the
	// daemons fail privs_init and watchfrr exits, crashlooping the cabinet.
	fmt.Fprintln(w, "    linux:")
	fmt.Fprintf(w, "      image: %s\n", frrImage)
	fmt.Fprintln(w, "      binds:")
	fmt.Fprintln(w, "        - configs/daemons:/etc/frr/daemons")
	fmt.Fprintln(w, "        - configs/snmpd.conf:/etc/snmp/snmpd.conf")
	fmt.Fprintln(w, "        - configs/wrapper.sh:/wrapper.sh")
	fmt.Fprintln(w, "      cmd: /wrapper.sh")
	fmt.Fprintln(w, "      cap-add:")
	fmt.Fprintln(w, "        - NET_ADMIN")
	fmt.Fprintln(w, "        - SYS_ADMIN")

	fmt.Fprintln(w, "  nodes:")
	for _, n := range spec.Nodes {
		fmt.Fprintf(w, "    %s:\n", n.Name)
		switch n.Kind {
		case "srlinux":
			fmt.Fprintln(w, "      kind: nokia_srlinux")
			fmt.Fprintf(w, "      startup-config: configs/%s\n", configFilename(n))
		case "frr":
			fmt.Fprintln(w, "      kind: linux")
			fmt.Fprintln(w, "      binds:")
			fmt.Fprintf(w, "        - configs/%s:/etc/frr/frr.conf\n", configFilename(n))
		}
	}

	fmt.Fprintln(w, "  links:")
	for _, l := range spec.Links {
		na := spec.NodeByName(l.A.Node)
		nb := spec.NodeByName(l.B.Node)
		fmt.Fprintf(w, "    - endpoints: [%q, %q]\n",
			fmt.Sprintf("%s:%s", l.A.Node, clabIntf(na.Kind, l.A.Intf)),
			fmt.Sprintf("%s:%s", l.B.Node, clabIntf(nb.Kind, l.B.Intf)),
		)
	}
	return nil
}

// WriteTopology writes the clabernetes Topology CR with embedded containerlab
// definition and per-node filesFromConfigMap mounts for startup configs.
func WriteTopology(w io.Writer, spec *Spec) error {
	var clab strings.Builder
	if err := WriteContainerlab(&clab, spec); err != nil {
		return err
	}

	fmt.Fprintln(w, "apiVersion: clabernetes.containerlab.dev/v1alpha1")
	fmt.Fprintln(w, "kind: Topology")
	fmt.Fprintln(w, "metadata:")
	fmt.Fprintf(w, "  name: %s\n", spec.Metadata.Name)
	fmt.Fprintln(w, "spec:")
	// Expose only via ClusterIP services. Default LoadBalancer makes k3d's
	// klipper-lb fan out into per-node DaemonSet pods that port-collide
	// (12 pods × shared host ports), leaving dozens of svclb pods Pending.
	// gnmic + cut-fiber reach gNMI in-cluster, so ClusterIP is sufficient.
	fmt.Fprintln(w, "  expose:")
	fmt.Fprintln(w, "    exposeType: ClusterIP")
	fmt.Fprintln(w, "  definition:")
	fmt.Fprintln(w, "    containerlab: |-")
	for _, line := range strings.Split(strings.TrimRight(clab.String(), "\n"), "\n") {
		fmt.Fprintf(w, "      %s\n", line)
	}
	fmt.Fprintln(w, "  deployment:")
	fmt.Fprintln(w, "    filesFromConfigMap:")
	// The clabernetes admission webhook defaults `mode: read` on any file
	// entry that omits it, so emit it explicitly to keep ArgoCD diffs clean.
	for _, n := range spec.Nodes {
		fmt.Fprintf(w, "      %s:\n", n.Name)
		fmt.Fprintf(w, "        - filePath: configs/%s\n", configFilename(n))
		fmt.Fprintln(w, "          configMapName: topology-startup-configs")
		fmt.Fprintf(w, "          configMapPath: %s\n", configFilename(n))
		fmt.Fprintln(w, "          mode: read")
		if n.Kind == "frr" {
			fmt.Fprintln(w, "        - filePath: configs/daemons")
			fmt.Fprintln(w, "          configMapName: topology-startup-configs")
			fmt.Fprintln(w, "          configMapPath: daemons")
			fmt.Fprintln(w, "          mode: read")
			fmt.Fprintln(w, "        - filePath: configs/snmpd.conf")
			fmt.Fprintln(w, "          configMapName: topology-startup-configs")
			fmt.Fprintln(w, "          configMapPath: snmpd.conf")
			fmt.Fprintln(w, "          mode: read")
			fmt.Fprintln(w, "        - filePath: configs/wrapper.sh")
			fmt.Fprintln(w, "          configMapName: topology-startup-configs")
			fmt.Fprintln(w, "          configMapPath: wrapper.sh")
			fmt.Fprintln(w, "          mode: execute")
		}
	}
	return nil
}

// WriteTopologyKustomization writes a kustomization.yaml that bundles the
// Topology CR with a single ConfigMap containing every startup-config.
func WriteTopologyKustomization(w io.Writer, spec *Spec) error {
	fmt.Fprintln(w, "apiVersion: kustomize.config.k8s.io/v1beta1")
	fmt.Fprintln(w, "kind: Kustomization")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "namespace: clabernetes")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "resources:")
	fmt.Fprintln(w, "  - topology.yaml")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "generatorOptions:")
	fmt.Fprintln(w, "  disableNameSuffixHash: true")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "configMapGenerator:")
	fmt.Fprintln(w, "  - name: topology-startup-configs")
	fmt.Fprintln(w, "    files:")

	files := make([]string, 0, len(spec.Nodes)+1)
	for _, n := range spec.Nodes {
		files = append(files, "startup-configs/"+configFilename(n))
	}
	files = append(files, "startup-configs/daemons", "startup-configs/snmpd.conf", "startup-configs/wrapper.sh")
	sort.Strings(files)
	for _, f := range files {
		fmt.Fprintf(w, "      - %s\n", f)
	}
	return nil
}

// WriteFRRDaemons writes the standard FRR daemons file enabling zebra+bgpd.
func WriteFRRDaemons(w io.Writer) error {
	_, err := io.WriteString(w, frrDaemons)
	return err
}
