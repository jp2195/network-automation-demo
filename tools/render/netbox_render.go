package main

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

type netboxSeed struct {
	CustomFields   []nbCustomField  `json:"custom_fields"`
	Regions        []nbRegion       `json:"regions"`
	SiteGroups     []nbSiteGroup    `json:"site_groups"`
	RIRs           []nbRIR          `json:"rirs"`
	ASNs           []nbASN          `json:"asns"`
	Sites          []nbSite         `json:"sites"`
	Tenants        []nbTenant       `json:"tenants"`
	OwnerGroups    []nbOwnerGroup   `json:"owner_groups"`
	Owners         []nbOwner        `json:"owners"`
	Manufacturers  []nbManufacturer `json:"manufacturers"`
	DeviceTypes    []nbDeviceType   `json:"device_types"`
	DeviceRoles    []nbDeviceRole   `json:"device_roles"`
	Devices        []nbDevice       `json:"devices"`
	Interfaces     []nbInterface    `json:"interfaces"`
	IPAddresses    []nbIPAddress    `json:"ip_addresses"`
	Cables         []nbCable        `json:"cables"`
	JournalEntries []nbJournalEntry `json:"journal_entries"`
}

// nbOwnerGroup / nbOwner mirror NetBox 4.6's Ownership model
// (/api/users/owner-groups/ and /api/users/owners/). OwnerGroup has no slug
// in NetBox's schema, so seeders look up by name.
type nbOwnerGroup struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

type nbOwner struct {
	Name        string `json:"name"`
	Group       string `json:"group,omitempty"` // OwnerGroup name
	Description string `json:"description,omitempty"`
}

// nbCustomField mirrors NetBox 4.x's /api/extras/custom-fields/ schema. The
// `object_types` field replaces the legacy `content_types` (≤ NetBox 3.x).
type nbCustomField struct {
	Name        string   `json:"name"`
	Label       string   `json:"label"`
	Type        string   `json:"type"`
	ObjectTypes []string `json:"object_types"`
	Description string   `json:"description,omitempty"`
}

type nbRegion struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

type nbSiteGroup struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

type nbRIR struct {
	Slug      string `json:"slug"`
	Name      string `json:"name"`
	IsPrivate bool   `json:"is_private"`
}

type nbASN struct {
	ASN         int      `json:"asn"`
	RIR         string   `json:"rir"`             // slug
	Sites       []string `json:"sites,omitempty"` // site slugs
	Description string   `json:"description,omitempty"`
}

type nbSite struct {
	Slug      string  `json:"slug"`
	Name      string  `json:"name"`
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Region    string  `json:"region,omitempty"` // slug
	Group     string  `json:"group,omitempty"`  // slug
}

type nbTenant struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
}

type nbProvider struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
}

type nbManufacturer struct {
	Slug string `json:"slug"`
	Name string `json:"name"`
}

type nbDeviceType struct {
	Manufacturer string `json:"manufacturer"`
	Model        string `json:"model"`
	Slug         string `json:"slug"`
}

type nbDeviceRole struct {
	Slug  string `json:"slug"`
	Name  string `json:"name"`
	Color string `json:"color"`
}

type nbDevice struct {
	Name         string                 `json:"name"`
	Site         string                 `json:"site"`
	Role         string                 `json:"role"`
	DeviceType   string                 `json:"device_type"`
	Status       string                 `json:"status"`
	PrimaryIP4   string                 `json:"primary_ip4,omitempty"`
	Tenants      []string               `json:"tenants,omitempty"`
	CustomFields map[string]interface{} `json:"custom_fields,omitempty"`
}

type nbInterface struct {
	Device      string `json:"device"`
	Name        string `json:"name"`
	Type        string `json:"type"`
	Description string `json:"description,omitempty"`
}

type nbIPAddress struct {
	Address   string `json:"address"`
	Device    string `json:"device"`
	Interface string `json:"interface"`
}

type nbCable struct {
	Label        string                 `json:"label"`
	A            nbCableTermination     `json:"a"`
	B            nbCableTermination     `json:"b"`
	Status       string                 `json:"status"`
	Type         string                 `json:"type,omitempty"`
	Owner        string                 `json:"owner,omitempty"` // Owner name
	Description  string                 `json:"description,omitempty"`
	Length       float64                `json:"length,omitempty"`
	LengthUnit   string                 `json:"length_unit,omitempty"`
	InstallDate  string                 `json:"install_date,omitempty"`
	CustomFields map[string]interface{} `json:"custom_fields,omitempty"`
}

type nbCableTermination struct {
	Device    string `json:"device"`
	Interface string `json:"interface"`
}

type nbJournalEntry struct {
	AssignedObjectType string                 `json:"assigned_object_type"`
	AssignedObject     map[string]interface{} `json:"assigned_object"`
	Kind               string                 `json:"kind"`
	Comments           string                 `json:"comments"`
}

func WriteNetBox(w io.Writer, s *Spec) error {
	out := netboxSeed{}

	dev := []string{"dcim.device"}
	cab := []string{"dcim.cable"}
	out.CustomFields = []nbCustomField{
		{Name: "isis_sid", Label: "IS-IS SID", Type: "integer", ObjectTypes: dev, Description: "Segment Routing prefix-SID"},
		{Name: "circuit_id", Label: "Circuit ID", Type: "text", ObjectTypes: cab, Description: "Provider circuit / OSP identifier"},
		{Name: "restoration_sla_hours", Label: "Restoration SLA (h)", Type: "integer", ObjectTypes: cab, Description: "Provider restoration SLA in hours"},
	}

	// Regions: one per district. Sites inherit district via region membership.
	districts := uniqueDistricts(s)
	for _, d := range districts {
		out.Regions = append(out.Regions, nbRegion{
			Slug: fmt.Sprintf("district-%d", d),
			Name: fmt.Sprintf("District %d", d),
		})
	}

	// Site Groups: one per non-empty corridor. Cables span 2 sites in the
	// same corridor, so the corridor is derivable from either endpoint —
	// no need for a corridor field on cables.
	corridors := uniqueCorridors(s)
	for _, c := range corridors {
		out.SiteGroups = append(out.SiteGroups, nbSiteGroup{
			Slug:        slugify(c),
			Name:        c,
			Description: "Highway corridor (Atlas DOT Region 7)",
		})
	}

	// Private RIR for the cabinet ASNs (RFC 6996 64512–65534 range).
	out.RIRs = []nbRIR{
		{Slug: "atlas-private", Name: "Atlas Private", IsPrivate: true},
	}

	// One ASN object per FRR cabinet, each assigned to the cabinet's site.
	for _, n := range s.Nodes {
		if n.Kind != "frr" || n.ASN == 0 {
			continue
		}
		out.ASNs = append(out.ASNs, nbASN{
			ASN:         n.ASN,
			RIR:         "atlas-private",
			Sites:       []string{n.Site.Label},
			Description: fmt.Sprintf("BGP ASN for cabinet %s", n.Name),
		})
	}

	for _, n := range s.Nodes {
		site := nbSite{
			Slug:      n.Site.Label,
			Name:      n.Site.City,
			Latitude:  n.Site.Lat,
			Longitude: n.Site.Lon,
			Region:    fmt.Sprintf("district-%d", n.Site.District),
		}
		if n.Site.Corridor != "" {
			site.Group = slugify(n.Site.Corridor)
		}
		out.Sites = append(out.Sites, site)
	}

	for _, a := range s.Agencies {
		out.Tenants = append(out.Tenants, nbTenant{Slug: a.Slug, Name: a.Name})
	}

	// Spec providers map to NetBox 4.6 Owners (asset-ownership model). One
	// OwnerGroup gathers all OSP fiber lessors so the cabinet/ring cables
	// can populate cable.owner directly instead of via a custom field.
	out.OwnerGroups = []nbOwnerGroup{
		{Name: "OSP Providers", Description: "Outside-plant fiber lessors and operator-owned plant"},
	}
	for _, p := range s.Providers {
		out.Owners = append(out.Owners, nbOwner{
			Name:        p.Name,
			Group:       "OSP Providers",
			Description: "Fiber owner for cabinet/ring OSP cables",
		})
	}

	out.Manufacturers = []nbManufacturer{
		{Slug: "nokia", Name: "Nokia"},
		{Slug: "frrouting", Name: "FRRouting"},
		{Slug: "atlas-vision", Name: "Atlas Vision"},
		{Slug: "atlas-traffic", Name: "Atlas Traffic Systems"},
	}
	out.DeviceTypes = []nbDeviceType{
		{Manufacturer: "nokia", Model: "SR Linux", Slug: "srlinux"},
		{Manufacturer: "frrouting", Model: "FRR Routing Stack", Slug: "frr"},
		{Manufacturer: "atlas-vision", Model: "AV-CCTV-HD", Slug: "av-cctv-hd"},
		{Manufacturer: "atlas-traffic", Model: "AT-SIG-2070", Slug: "at-sig-2070"},
		{Manufacturer: "atlas-traffic", Model: "AT-DMS-VMS16", Slug: "at-dms-vms16"},
		{Manufacturer: "atlas-traffic", Model: "AT-RAMP-MID", Slug: "at-ramp-mid"},
	}
	out.DeviceRoles = []nbDeviceRole{
		{Slug: "tmc", Name: "TMC", Color: "ff0000"},
		{Slug: "corridor-hub", Name: "Corridor Hub", Color: "0066ff"},
		{Slug: "field-cabinet", Name: "Field Cabinet", Color: "00aa00"},
		{Slug: "cctv-camera", Name: "CCTV Camera", Color: "9c27b0"},
		{Slug: "signal-controller", Name: "Signal Controller", Color: "ff9800"},
		{Slug: "dms", Name: "DMS", Color: "795548"},
		{Slug: "ramp-meter", Name: "Ramp Meter", Color: "607d8b"},
	}

	// Routers (SR Linux + FRR cabinets).
	for _, n := range s.Nodes {
		deviceType := "srlinux"
		if n.Kind == "frr" {
			deviceType = "frr"
		}
		out.Devices = append(out.Devices, nbDevice{
			Name:         n.Name,
			Site:         n.Site.Label,
			Role:         n.Role,
			DeviceType:   deviceType,
			Status:       "active",
			PrimaryIP4:   n.LoopbackV4 + "/32",
			Tenants:      n.Agencies,
			CustomFields: map[string]interface{}{"isis_sid": n.ISISSID},
		})
	}

	// ITS assets per cabinet (cameras, signal controllers, DMS, ramp meters).
	// Each gets its own NetBox device record at the cabinet's site so the
	// cabinet's roster is observable via Site → Devices instead of a CF count.
	for _, n := range s.Nodes {
		if n.Kind != "frr" {
			continue
		}
		out.Devices = append(out.Devices, itsDevices(n)...)
	}

	for _, n := range s.Nodes {
		loName := "lo0"
		if n.Kind == "frr" {
			loName = "lo"
		}
		out.Interfaces = append(out.Interfaces, nbInterface{
			Device:      n.Name,
			Name:        loName,
			Type:        "virtual",
			Description: "Loopback (router-id, IS-IS source)",
		})
		for _, ifc := range s.InterfacesOf(n.Name) {
			ifaceType := "10gbase-x-sfpp"
			if n.Kind == "frr" {
				// Cabinet drops are GigE copper; "1000base-t" keeps the type
				// physical so NetBox 4.x accepts cable terminations on it.
				ifaceType = "1000base-t"
			}
			desc := "to " + ifc.PeerNode + "/" + ifc.PeerIntf + " [" + ifc.Cable.Label + "]"
			out.Interfaces = append(out.Interfaces, nbInterface{
				Device:      n.Name,
				Name:        ifc.Name,
				Type:        ifaceType,
				Description: desc,
			})
		}
	}

	for _, n := range s.Nodes {
		loName := "lo0"
		if n.Kind == "frr" {
			loName = "lo"
		}
		out.IPAddresses = append(out.IPAddresses, nbIPAddress{
			Address:   n.LoopbackV4 + "/32",
			Device:    n.Name,
			Interface: loName,
		})
	}
	for _, n := range s.Nodes {
		for _, ifc := range s.InterfacesOf(n.Name) {
			out.IPAddresses = append(out.IPAddresses, nbIPAddress{
				Address:   ifc.LocalV4 + "/30",
				Device:    n.Name,
				Interface: ifc.Name,
			})
		}
	}

	providerNameBySlug := map[string]string{}
	for _, p := range s.Providers {
		providerNameBySlug[p.Slug] = p.Name
	}
	for _, l := range s.Links {
		out.Cables = append(out.Cables, nbCable{
			Label:       l.Cable.Label,
			A:           nbCableTermination{Device: l.A.Node, Interface: l.A.Intf},
			B:           nbCableTermination{Device: l.B.Node, Interface: l.B.Intf},
			Status:      "connected",
			Type:        "smf-os2",
			Owner:       providerNameBySlug[l.Cable.Provider],
			Description: l.Cable.RouteDescription,
			Length:      l.Cable.LengthKm,
			LengthUnit:  "km",
			InstallDate: l.Cable.Installed,
			CustomFields: map[string]interface{}{
				"circuit_id":            l.Cable.CircuitID,
				"restoration_sla_hours": l.Cable.RestorationSLAHours,
			},
		})
	}

	out.JournalEntries = []nbJournalEntry{
		{
			AssignedObjectType: "dcim.cable",
			AssignedObject:     map[string]interface{}{"label": "FOC-NW-01"},
			Kind:               "info",
			Comments:           "2024-08-12: prior splice failure during I-75 widening; resolved 4h22m by Apex Fiber crew. Reference incident IR-2024-0814.",
		},
		{
			AssignedObjectType: "dcim.cable",
			AssignedObject:     map[string]interface{}{"label": "FOC-RING-NWN"},
			Kind:               "warning",
			Comments:           "2025-02-03: scheduled corridor maintenance (I-285 paving); 90-minute window 02:00-03:30 EDT, no traffic impact (TI-LFA absorbed reroute).",
		},
		{
			AssignedObjectType: "dcim.cable",
			AssignedObject:     map[string]interface{}{"label": "FOC-CAB-NW"},
			Kind:               "info",
			Comments:           "2025-04-19: cabinet drop hand-off acceptance; OTDR baseline recorded by Pinecrest Public Works.",
		},
	}

	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

// uniqueDistricts returns the sorted set of district numbers found across
// all nodes' sites in the spec.
func uniqueDistricts(s *Spec) []int {
	seen := map[int]bool{}
	for _, n := range s.Nodes {
		if n.Site.District != 0 {
			seen[n.Site.District] = true
		}
	}
	out := make([]int, 0, len(seen))
	for d := range seen {
		out = append(out, d)
	}
	sort.Ints(out)
	return out
}

// uniqueCorridors returns the sorted set of non-empty corridor names found
// across all nodes' sites in the spec.
func uniqueCorridors(s *Spec) []string {
	seen := map[string]bool{}
	for _, n := range s.Nodes {
		if n.Site.Corridor != "" {
			seen[n.Site.Corridor] = true
		}
	}
	out := make([]string, 0, len(seen))
	for c := range seen {
		out = append(out, c)
	}
	sort.Strings(out)
	return out
}

// slugify lowercases and replaces non-alnum runs with single dashes — good
// enough for the short corridor names in the spec.
func slugify(name string) string {
	var b strings.Builder
	prevDash := true
	for _, r := range strings.ToLower(name) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			prevDash = false
		default:
			if !prevDash {
				b.WriteByte('-')
				prevDash = true
			}
		}
	}
	return strings.Trim(b.String(), "-")
}

// itsDevices generates one nbDevice per asset declared in a cabinet's
// ITSInventory. Names follow <cabinet>-<class>-NN (e.g. fc-nw-cctv-01).
func itsDevices(n Node) []nbDevice {
	type spec struct {
		role       string
		deviceType string
		count      int
		shortName  string
	}
	specs := []spec{
		{"cctv-camera", "av-cctv-hd", n.ITSInventory.CCTV, "cctv"},
		{"signal-controller", "at-sig-2070", n.ITSInventory.SignalControllers, "sig"},
		{"dms", "at-dms-vms16", n.ITSInventory.DMS, "dms"},
		{"ramp-meter", "at-ramp-mid", n.ITSInventory.RampMeters, "rm"},
	}
	var out []nbDevice
	for _, sp := range specs {
		for i := 1; i <= sp.count; i++ {
			out = append(out, nbDevice{
				Name:       fmt.Sprintf("%s-%s-%02d", n.Name, sp.shortName, i),
				Site:       n.Site.Label,
				Role:       sp.role,
				DeviceType: sp.deviceType,
				Status:     "active",
			})
		}
	}
	return out
}
