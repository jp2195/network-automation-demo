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
	"net/http"
	"net/url"
	"os"
	"strconv"
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

func (s *server) forward(w http.ResponseWriter, r *http.Request, endpoint string, allow map[string]bool) {
	var body map[string]any
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "bad json"})
		return
	}
	act, _ := body["action"].(string)
	if !allow[act] {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "invalid action"})
		return
	}
	buf, _ := json.Marshal(body)
	resp, err := s.client.Post(s.webhook+endpoint, "application/json", bytes.NewReader(buf))
	if err != nil {
		writeJSON(w, 502, map[string]any{"ok": false, "detail": err.Error()})
		return
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	writeJSON(w, 200, map[string]any{"ok": resp.StatusCode < 300, "upstream": resp.StatusCode})
}

func (s *server) handleCut(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/manual-cut", map[string]bool{"disable": true, "enable": true})
}

func (s *server) handleGray(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/gray-failure", map[string]bool{"start": true, "end": true})
}

func (s *server) handleMaintenance(w http.ResponseWriter, r *http.Request) {
	s.forward(w, r, "/maintenance", map[string]bool{"start": true, "end": true})
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
	s := &server{
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
