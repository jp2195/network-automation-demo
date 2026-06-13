package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestWriteConsoleTargets(t *testing.T) {
	s, err := LoadSpec("../../spec/atlanta.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	if err := WriteConsoleTargets(&buf, s); err != nil {
		t.Fatal(err)
	}
	var ct struct {
		Nodes []struct {
			Name       string   `json:"name"`
			Kind       string   `json:"kind"`
			Role       string   `json:"role"`
			Interfaces []string `json:"interfaces"`
		} `json:"nodes"`
		Links []struct {
			ID string `json:"id"`
		} `json:"links"`
	}
	if err := json.Unmarshal(buf.Bytes(), &ct); err != nil {
		t.Fatalf("not valid json: %v", err)
	}
	byName := map[string][]string{}
	for _, n := range ct.Nodes {
		byName[n.Name] = n.Interfaces
	}
	if _, ok := byName["hub-i20e"]; !ok {
		t.Errorf("hub-i20e missing from targets")
	}
	if !contains(byName["hub-i20e"], "ethernet-1/4") {
		t.Errorf("hub-i20e should expose ethernet-1/4, got %v", byName["hub-i20e"])
	}
	if _, ok := byName["fc-i20e"]; !ok {
		t.Errorf("fc-i20e cabinet missing from targets")
	}
	ids := func() string {
		var b strings.Builder
		for _, l := range ct.Links {
			b.WriteString(l.ID + " ")
		}
		return b.String()
	}()
	if !strings.Contains(ids, "ring-e-i20e") {
		t.Errorf("link ring-e-i20e missing; got %s", ids)
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
