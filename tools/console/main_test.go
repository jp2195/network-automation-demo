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
	tg, err := loadTargets()
	if err != nil {
		panic(err)
	}
	return &server{webhook: webhook, prom: prom, argo: argo,
		client: &http.Client{}, targets: tg}
}

func jsonReq(method, path, body string) *http.Request {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req
}

// captureUpstream returns a webhook stub and a pointer to the last
// path+decoded body it received.
func captureUpstream(t *testing.T) (*httptest.Server, *string, *map[string]any) {
	t.Helper()
	var path string
	var body map[string]any
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		b, _ := io.ReadAll(r.Body)
		body = nil
		json.Unmarshal(b, &body)
		w.WriteHeader(200)
	}))
	t.Cleanup(up.Close)
	return up, &path, &body
}

func TestForwardCutValid(t *testing.T) {
	up, path, body := captureUpstream(t)
	s := newTestServer(up.URL, "", "")
	rec := httptest.NewRecorder()
	s.handleCut(rec, jsonReq("POST", "/api/cut",
		`{"node":"hub-e","interface":"ethernet-1/1","action":"disable","extra":"x"}`))
	if rec.Code != 200 {
		t.Fatalf("want 200, got %d (%s)", rec.Code, rec.Body)
	}
	if *path != "/manual-cut" {
		t.Errorf("upstream path %q", *path)
	}
	want := map[string]any{"node": "hub-e", "interface": "ethernet-1/1", "action": "disable"}
	if len(*body) != len(want) {
		t.Errorf("upstream body has unexpected keys: %v", *body)
	}
	for k, v := range want {
		if (*body)[k] != v {
			t.Errorf("upstream %s = %v, want %v", k, (*body)[k], v)
		}
	}
}

func TestForwardRejectsBadAction(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	rec := httptest.NewRecorder()
	s.handleCut(rec, jsonReq("POST", "/api/cut",
		`{"node":"hub-e","interface":"ethernet-1/1","action":"explode"}`))
	if rec.Code != 400 {
		t.Errorf("want 400 for bad action, got %d", rec.Code)
	}
}

func TestCutValidation(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	cases := map[string]string{
		"unknown node":      `{"node":"evil","interface":"ethernet-1/1","action":"disable"}`,
		"iface not on node": `{"node":"hub-i20w","interface":"ethernet-1/4","action":"disable"}`,
		"bad iface syntax":  `{"node":"hub-e","interface":"ethernet-1/1]/x","action":"disable"}`,
		"frr port not gnmi": `{"node":"fc-n","interface":"eth1","action":"disable"}`,
		"node wrong type":   `{"node":1,"interface":"ethernet-1/1","action":"disable"}`,
		"missing interface": `{"node":"hub-e","action":"disable"}`,
	}
	for name, b := range cases {
		rec := httptest.NewRecorder()
		s.handleCut(rec, jsonReq("POST", "/api/cut", b))
		if rec.Code != 400 {
			t.Errorf("%s: want 400, got %d", name, rec.Code)
		}
	}
}

// Every node/interface the UI scenarios hard-code must pass validation.
func TestCutAcceptsScenarioTargets(t *testing.T) {
	s := newTestServer("", "", "")
	for _, p := range [][2]string{
		{"hub-e", "ethernet-1/1"}, {"hub-i20e", "ethernet-1/2"},
		{"hub-i20e", "ethernet-1/4"}, {"tmc-2", "ethernet-1/2"},
	} {
		if _, err := buildCut(s.targets, map[string]any{"node": p[0], "interface": p[1]}); err != nil {
			t.Errorf("%v rejected: %v", p, err)
		}
	}
}

func TestGrayActionAllowlist(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	rec := httptest.NewRecorder()
	s.handleGray(rec, jsonReq("POST", "/api/gray", `{"link":"ring-e-i20e","action":"nope"}`))
	if rec.Code != 400 {
		t.Errorf("want 400, got %d", rec.Code)
	}
}

func TestGrayLinkValidation(t *testing.T) {
	up, path, body := captureUpstream(t)
	s := newTestServer(up.URL, "", "")
	for _, b := range []string{
		`{"link":"ring-x-y","action":"start"}`,
		`{"link":"ring-e-i20e; rm -rf /","action":"start"}`,
		`{"action":"start"}`,
	} {
		rec := httptest.NewRecorder()
		s.handleGray(rec, jsonReq("POST", "/api/gray", b))
		if rec.Code != 400 {
			t.Errorf("%s: want 400, got %d", b, rec.Code)
		}
	}
	rec := httptest.NewRecorder()
	s.handleGray(rec, jsonReq("POST", "/api/gray", `{"link":"hubn-fcn","action":"end","node":"x"}`))
	if rec.Code != 200 || *path != "/gray-failure" {
		t.Fatalf("valid gray: code %d path %q", rec.Code, *path)
	}
	if len(*body) != 2 || (*body)["link"] != "hubn-fcn" || (*body)["action"] != "end" {
		t.Errorf("upstream body %v", *body)
	}
}

func TestMaintenanceHoursAndComment(t *testing.T) {
	up, _, body := captureUpstream(t)
	s := newTestServer(up.URL, "", "")
	rec := httptest.NewRecorder()
	s.handleMaintenance(rec, jsonReq("POST", "/api/maintenance",
		`{"node":"hub-i20e","action":"start","hours":2,"comment":"</script>"}`))
	if rec.Code != 200 {
		t.Fatalf("want 200, got %d (%s)", rec.Code, rec.Body)
	}
	if (*body)["hours"] != float64(2) || (*body)["node"] != "hub-i20e" {
		t.Errorf("upstream body %v", *body)
	}
	if _, ok := (*body)["comment"]; ok {
		t.Errorf("comment must not be forwarded: %v", *body)
	}
	// numeric string is accepted (curl/scripts)
	rec = httptest.NewRecorder()
	s.handleMaintenance(rec, jsonReq("POST", "/api/maintenance",
		`{"node":"hub-i20e","action":"start","hours":"1.5"}`))
	if rec.Code != 200 || (*body)["hours"] != 1.5 {
		t.Errorf("string hours: code %d body %v", rec.Code, *body)
	}
	// end carries only node+action
	rec = httptest.NewRecorder()
	s.handleMaintenance(rec, jsonReq("POST", "/api/maintenance",
		`{"node":"hub-i20e","action":"end","hours":999}`))
	if rec.Code != 200 || len(*body) != 2 {
		t.Errorf("end: code %d body %v", rec.Code, *body)
	}
	for _, h := range []string{`0`, `-1`, `49`, `"abc"`, `"NaN"`, `true`, `[1]`} {
		rec := httptest.NewRecorder()
		s.handleMaintenance(rec, jsonReq("POST", "/api/maintenance",
			`{"node":"hub-i20e","action":"start","hours":`+h+`}`))
		if rec.Code != 400 {
			t.Errorf("hours=%s: want 400, got %d", h, rec.Code)
		}
	}
	rec = httptest.NewRecorder()
	s.handleMaintenance(rec, jsonReq("POST", "/api/maintenance", `{"node":"nope","action":"end"}`))
	if rec.Code != 400 {
		t.Errorf("unknown node: want 400, got %d", rec.Code)
	}
}

func TestForwardRequiresPostAndJSON(t *testing.T) {
	s := newTestServer("http://unused", "", "")
	rec := httptest.NewRecorder()
	s.handleCut(rec, httptest.NewRequest("GET", "/api/cut", nil))
	if rec.Code != 405 {
		t.Errorf("GET: want 405, got %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/cut",
		strings.NewReader(`{"node":"hub-e","interface":"ethernet-1/1","action":"disable"}`))
	req.Header.Set("Content-Type", "text/plain")
	s.handleCut(rec, req)
	if rec.Code != 415 {
		t.Errorf("text/plain: want 415, got %d", rec.Code)
	}
}

func TestForwardOriginCheck(t *testing.T) {
	up, _, _ := captureUpstream(t)
	s := newTestServer(up.URL, "", "")
	const b = `{"node":"hub-e","interface":"ethernet-1/1","action":"disable"}`

	req := jsonReq("POST", "/api/cut", b)
	req.Host = "console.local"
	req.Header.Set("Origin", "https://evil.example")
	rec := httptest.NewRecorder()
	s.handleCut(rec, req)
	if rec.Code != 403 {
		t.Errorf("cross-origin: want 403, got %d", rec.Code)
	}

	req = jsonReq("POST", "/api/cut", b)
	req.Host = "console.local:8080"
	req.Header.Set("Origin", "http://console.local:8080")
	rec = httptest.NewRecorder()
	s.handleCut(rec, req)
	if rec.Code != 200 {
		t.Errorf("same-origin: want 200, got %d", rec.Code)
	}
}

func TestForward502HidesUpstreamURL(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	addr := up.URL
	up.Close() // connection refused
	s := newTestServer(addr, "", "")
	rec := httptest.NewRecorder()
	s.handleCut(rec, jsonReq("POST", "/api/cut",
		`{"node":"hub-e","interface":"ethernet-1/1","action":"disable"}`))
	if rec.Code != 502 {
		t.Fatalf("want 502, got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), strings.TrimPrefix(addr, "http://")) {
		t.Errorf("502 body leaks upstream address: %s", rec.Body)
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
