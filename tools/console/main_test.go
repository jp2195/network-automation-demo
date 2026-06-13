package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(webhook, prom, argo string) *server {
	return &server{webhook: webhook, prom: prom, argo: argo,
		client: &http.Client{}}
}

func TestForwardCutValid(t *testing.T) {
	var got string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = r.URL.Path + " " + string(b)
		w.WriteHeader(200)
	}))
	defer up.Close()
	s := newTestServer(up.URL, "", "")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/cut",
		strings.NewReader(`{"node":"hub-e","interface":"ethernet-1/1","action":"disable"}`))
	s.handleCut(rec, req)
	if rec.Code != 200 {
		t.Fatalf("want 200, got %d (%s)", rec.Code, rec.Body)
	}
	if !strings.HasPrefix(got, "/manual-cut ") || !strings.Contains(got, `"node":"hub-e"`) {
		t.Errorf("upstream got %q", got)
	}
}

func TestForwardRejectsBadAction(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/cut",
		strings.NewReader(`{"node":"hub-e","interface":"ethernet-1/1","action":"explode"}`))
	s.handleCut(rec, req)
	if rec.Code != 400 {
		t.Errorf("want 400 for bad action, got %d", rec.Code)
	}
}

func TestGrayActionAllowlist(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/gray",
		strings.NewReader(`{"link":"ring-e-i20e","action":"nope"}`))
	s.handleGray(rec, req)
	if rec.Code != 400 {
		t.Errorf("want 400, got %d", rec.Code)
	}
}

func TestStatusMergesAndDegrades(t *testing.T) {
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"data":{"result":[{"value":[0,"3"]}]}}`)
	}))
	defer prom.Close()
	argo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
	}))
	defer argo.Close()
	s := newTestServer("", prom.URL, argo.URL)
	rec := httptest.NewRecorder()
	s.handleStatus(rec, httptest.NewRequest("GET", "/api/status", nil))
	if rec.Code != 200 {
		t.Fatalf("status want 200, got %d", rec.Code)
	}
	var out struct {
		LinksDown        int      `json:"links_down"`
		WorkflowsRunning int      `json:"workflows_running"`
		Degraded         []string `json:"degraded"`
	}
	json.Unmarshal(rec.Body.Bytes(), &out)
	if out.LinksDown != 3 {
		t.Errorf("links_down want 3, got %d", out.LinksDown)
	}
	if len(out.Degraded) == 0 {
		t.Errorf("argo failure should be recorded in degraded[]")
	}
}
