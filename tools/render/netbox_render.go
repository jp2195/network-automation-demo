package main

import (
	"encoding/json"
	"io"
)

type netboxSeed struct {
	Sites          []nbSite         `json:"sites"`
	Tenants        []nbTenant       `json:"tenants"`
	Providers      []nbProvider     `json:"providers"`
	Manufacturers  []nbManufacturer `json:"manufacturers"`
	DeviceTypes    []nbDeviceType   `json:"device_types"`
	DeviceRoles    []nbDeviceRole   `json:"device_roles"`
	Devices        []nbDevice       `json:"devices"`
	Interfaces     []nbInterface    `json:"interfaces"`
	IPAddresses    []nbIPAddress    `json:"ip_addresses"`
	Cables         []nbCable        `json:"cables"`
	JournalEntries []nbJournalEntry `json:"journal_entries"`
}

type nbSite struct {
	Slug         string                 `json:"slug"`
	Name         string                 `json:"name"`
	Latitude     float64                `json:"latitude"`
	Longitude    float64                `json:"longitude"`
	CustomFields map[string]interface{} `json:"custom_fields,omitempty"`
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

	for _, n := range s.Nodes {
		out.Sites = append(out.Sites, nbSite{
			Slug:      n.Site.Label,
			Name:      n.Site.City,
			Latitude:  n.Site.Lat,
			Longitude: n.Site.Lon,
			CustomFields: map[string]interface{}{
				"district": n.Site.District,
				"corridor": n.Site.Corridor,
			},
		})
	}

	for _, a := range s.Agencies {
		out.Tenants = append(out.Tenants, nbTenant{Slug: a.Slug, Name: a.Name})
	}
	for _, p := range s.Providers {
		out.Providers = append(out.Providers, nbProvider{Slug: p.Slug, Name: p.Name})
	}

	out.Manufacturers = []nbManufacturer{
		{Slug: "nokia", Name: "Nokia"},
		{Slug: "frrouting", Name: "FRRouting"},
	}
	out.DeviceTypes = []nbDeviceType{
		{Manufacturer: "nokia", Model: "SR Linux", Slug: "srlinux"},
		{Manufacturer: "frrouting", Model: "FRR Routing Stack", Slug: "frr"},
	}
	out.DeviceRoles = []nbDeviceRole{
		{Slug: "tmc", Name: "TMC", Color: "ff0000"},
		{Slug: "corridor-hub", Name: "Corridor Hub", Color: "0066ff"},
		{Slug: "field-cabinet", Name: "Field Cabinet", Color: "00aa00"},
	}

	for _, n := range s.Nodes {
		deviceType := "srlinux"
		if n.Kind == "frr" {
			deviceType = "frr"
		}
		cf := map[string]interface{}{
			"isis_sid": n.ISISSID,
		}
		if n.Kind == "frr" {
			cf["asn"] = n.ASN
			cf["cctv_count"] = n.ITSInventory.CCTV
			cf["signal_controllers"] = n.ITSInventory.SignalControllers
			cf["dms_count"] = n.ITSInventory.DMS
			cf["ramp_meters"] = n.ITSInventory.RampMeters
			cf["parent_hub"] = n.ParentHub
		}
		out.Devices = append(out.Devices, nbDevice{
			Name:         n.Name,
			Site:         n.Site.Label,
			Role:         n.Role,
			DeviceType:   deviceType,
			Status:       "active",
			PrimaryIP4:   n.LoopbackV4 + "/32",
			Tenants:      n.Agencies,
			CustomFields: cf,
		})
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
				ifaceType = "virtual"
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

	for _, l := range s.Links {
		out.Cables = append(out.Cables, nbCable{
			Label:  l.Cable.Label,
			A:      nbCableTermination{Device: l.A.Node, Interface: l.A.Intf},
			B:      nbCableTermination{Device: l.B.Node, Interface: l.B.Intf},
			Status: "connected",
			CustomFields: map[string]interface{}{
				"corridor":              l.Cable.Corridor,
				"route_description":     l.Cable.RouteDescription,
				"length_km":             l.Cable.LengthKm,
				"installed":             l.Cable.Installed,
				"provider":              l.Cable.Provider,
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
