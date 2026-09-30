// Command console is the scenario console: a single static page plus a
// thin proxy API. It POSTs button actions to the webhook EventSource and
// reads status from Prometheus + Argo. It holds no exec/device
// privileges — every action becomes an Argo Events Workflow downstream.
package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"io"
	"io/fs"
	"log"
	"math"
	"mime"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed static
var staticFS embed.FS

type server struct {
	webhook string
	prom    string
	argo    string
	client  *http.Client
	targets *targets
}

type httpStatusErr int

func (e httpStatusErr) Error() string { return "upstream status " + strconv.Itoa(int(e)) }

func errStatus(code int) error { return httpStatusErr(code) }

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("writeJSON encode: %v", err)
	}
}

// targets is the embedded console-targets.json allowlist. Every node,
// node:interface pair and link id the console forwards must appear here.
type targets struct {
	nodes map[string]bool
	ports map[string]bool // "node:interface"
	links map[string]bool
}

func loadTargets() (*targets, error) {
	raw, err := staticFS.ReadFile("static/console-targets.json")
	if err != nil {
		return nil, err
	}
	var doc struct {
		Nodes []struct {
			Name       string   `json:"name"`
			Interfaces []string `json:"interfaces"`
		} `json:"nodes"`
		Links []struct {
			ID string `json:"id"`
		} `json:"links"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	t := &targets{nodes: map[string]bool{}, ports: map[string]bool{}, links: map[string]bool{}}
	for _, n := range doc.Nodes {
		t.nodes[n.Name] = true
		for _, i := range n.Interfaces {
			t.ports[n.Name+":"+i] = true
		}
	}
	for _, l := range doc.Links {
		t.links[l.ID] = true
	}
	return t, nil
}

var (
	// Only SR Linux ports are cuttable (the cut WFT drives gNMI
	// admin-state); FRR cabinet eth1 ports are deliberately excluded.
	ifaceRe = regexp.MustCompile(`^ethernet-1/\d+$`)
	linkRe  = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
)

// maxMaintHours matches the max on the console's Hours input (index.html).
const maxMaintHours = 48

type reqError string

func (e reqError) Error() string { return string(e) }

// sameOrigin rejects cross-site browser POSTs: when an Origin header is
// present its host must equal the request Host. Non-browser clients
// (curl, scripts) send no Origin and are allowed through.
func sameOrigin(r *http.Request) bool {
	o := r.Header.Get("Origin")
	if o == "" {
		return true
	}
	u, err := url.Parse(o)
	if err != nil || u.Host == "" {
		return false
	}
	return strings.EqualFold(u.Host, r.Host)
}

// builder turns a decoded client body into the exact payload the upstream
// sensor expects, copying only validated, expected keys.
type builder func(t *targets, in map[string]any) (map[string]any, error)

func str(in map[string]any, k string) string {
	v, _ := in[k].(string)
	return v
}

func buildCut(t *targets, in map[string]any) (map[string]any, error) {
	node, iface := str(in, "node"), str(in, "interface")
	if !t.nodes[node] {
		return nil, reqError("unknown node")
	}
	if !ifaceRe.MatchString(iface) || !t.ports[node+":"+iface] {
		return nil, reqError("invalid interface")
	}
	return map[string]any{"node": node, "interface": iface}, nil
}

func buildGray(t *targets, in map[string]any) (map[string]any, error) {
	link := str(in, "link")
	if !linkRe.MatchString(link) || !t.links[link] {
		return nil, reqError("unknown link")
	}
	return map[string]any{"link": link}, nil
}

func buildMaintenance(t *targets, in map[string]any) (map[string]any, error) {
	node := str(in, "node")
	if !t.nodes[node] {
		return nil, reqError("unknown node")
	}
	out := map[string]any{"node": node}
	if str(in, "action") != "start" {
		return out, nil
	}
	var h float64
	switch v := in["hours"].(type) {
	case nil:
		return out, nil // sensor default applies
	case float64:
		h = v
	case string:
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return nil, reqError("invalid hours")
		}
		h = f
	default:
		return nil, reqError("invalid hours")
	}
	if math.IsNaN(h) || h <= 0 || h > maxMaintHours {
		return nil, reqError("invalid hours")
	}
	out["hours"] = h
	// Free-text comment is intentionally dropped: the UI never sends one
	// and the sensor supplies a fixed default.
	return out, nil
}

func (s *server) forward(w http.ResponseWriter, r *http.Request, endpoint string, allow map[string]bool, build builder) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeJSON(w, 405, map[string]any{"ok": false, "detail": "method not allowed"})
		return
	}
	if mt, _, err := mime.ParseMediaType(r.Header.Get("Content-Type")); err != nil || mt != "application/json" {
		writeJSON(w, 415, map[string]any{"ok": false, "detail": "content-type must be application/json"})
		return
	}
	if !sameOrigin(r) {
		writeJSON(w, 403, map[string]any{"ok": false, "detail": "cross-origin request rejected"})
		return
	}
	var in map[string]any
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&in); err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "bad json"})
		return
	}
	act := str(in, "action")
	if !allow[act] {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "invalid action"})
		return
	}
	body, err := build(s.targets, in)
	if err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": err.Error()})
		return
	}
	body["action"] = act
	buf, _ := json.Marshal(body)
	resp, err := s.client.Post(s.webhook+endpoint, "application/json", bytes.NewReader(buf))
	if err != nil {
		// Log the detail (it carries the in-cluster URL); return a generic error.
		log.Printf("forward %s: %v", endpoint, err)
		writeJSON(w, 502, map[string]any{"ok": false, "detail": "upstream unavailable"})
		return
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	writeJSON(w, 200, map[string]any{"ok": resp.StatusCode < 300, "upstream": resp.StatusCode})
}

func (s *server) handleCut(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/manual-cut", map[string]bool{"disable": true, "enable": true}, buildCut)
}

func (s *server) handleGray(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/gray-failure", map[string]bool{"start": true, "end": true}, buildGray)
}

func (s *server) handleMaintenance(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/maintenance", map[string]bool{"start": true, "end": true}, buildMaintenance)
}

func (s *server) promScalar(query string) (int, error) {
	u := s.prom + "/api/v1/query?query=" + url.QueryEscape(query)
	resp, err := s.client.Get(u)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, errStatus(resp.StatusCode)
	}
	var pr struct {
		Data struct {
			Result []struct {
				Value [2]any `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&pr); err != nil {
		return 0, err
	}
	if len(pr.Data.Result) == 0 {
		return 0, nil
	}
	str, _ := pr.Data.Result[0].Value[1].(string)
	f, _ := strconv.ParseFloat(str, 64)
	return int(f), nil
}

func (s *server) argoRunning() (int, error) {
	resp, err := s.client.Get(s.argo + "/api/v1/workflows/argo-events")
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, errStatus(resp.StatusCode)
	}
	var wl struct {
		Items []struct {
			Status struct {
				Phase string `json:"phase"`
			} `json:"status"`
		} `json:"items"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wl); err != nil {
		return 0, err
	}
	n := 0
	for _, it := range wl.Items {
		if it.Status.Phase == "Running" {
			n++
		}
	}
	return n, nil
}

func (s *server) handleStatus(w http.ResponseWriter, r *http.Request) {
	// Fan the four upstream calls out concurrently: under a degraded
	// cluster a sequential walk could exceed the browser's 5s poll
	// interval (4 × client timeout) and pile up requests. Each probe
	// writes its own field; failures land in degraded[]. Run in parallel,
	// then assemble — no shared-map races.
	type probe struct {
		field string
		fn    func() (int, error)
	}
	probes := []probe{
		{"nodes_up", func() (int, error) {
			return s.promScalar(`count(count by (node)(srl_nokia_interfaces_interface_oper_state == 1)) or vector(0)`)
		}},
		{"links_down", func() (int, error) {
			return s.promScalar(`count(link:oper_state_with_meta == 2) or vector(0)`)
		}},
		{"alerts_firing", func() (int, error) {
			return s.promScalar(`count(ALERTS{alertstate="firing",alertname!="Watchdog",alertname!="InfoInhibitor"}) or vector(0)`)
		}},
		{"workflows_running", s.argoRunning},
	}
	type result struct {
		field string
		val   int
		err   error
	}
	results := make([]result, len(probes))
	var wg sync.WaitGroup
	for i, p := range probes {
		wg.Add(1)
		go func(i int, p probe) {
			defer wg.Done()
			v, err := p.fn()
			results[i] = result{p.field, v, err}
		}(i, p)
	}
	wg.Wait()

	out := map[string]any{}
	degraded := []string{}
	for _, r := range results {
		if r.err == nil {
			out[r.field] = r.val
		} else {
			degraded = append(degraded, r.field)
		}
	}
	out["degraded"] = degraded
	writeJSON(w, 200, out)
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		log.Fatalf("static embed: %v", err)
	}
	mux.Handle("/", http.FileServer(http.FS(sub)))
	mux.HandleFunc("/api/cut", s.handleCut)
	mux.HandleFunc("/api/gray", s.handleGray)
	mux.HandleFunc("/api/maintenance", s.handleMaintenance)
	mux.HandleFunc("/api/status", s.handleStatus)
	return mux
}

func main() {
	tg, err := loadTargets()
	if err != nil {
		log.Fatalf("console-targets.json: %v", err)
	}
	s := &server{
		targets: tg,
		webhook: envOr("WEBHOOK_URL", "http://webhook-eventsource-svc.argo-events.svc.cluster.local:12000"),
		prom:    envOr("PROM_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"),
		argo:    envOr("ARGO_API", "http://argo-workflows-server.argo.svc.cluster.local:2746"),
		client:  &http.Client{Timeout: 8 * time.Second},
	}
	addr := envOr("LISTEN_ADDR", ":8080")
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.routes(),
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("scenario console listening on %s", addr)
	log.Fatal(srv.ListenAndServe())
}
