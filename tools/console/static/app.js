/* ============================================================
   Atlas DOT — Scenario Console : engine (v2)
   Real-wired: inventory from console-targets.json, status from
   /api/status (polled every 5s), actions via /api/cut|gray|maintenance.
   No simulation — all tile state comes from the real backend.
   ============================================================ */
(function () {
  "use strict";

  /* ---------------- inventory (loaded from server) ---------------- */
  let BACKBONE_NODES = [];  // kind === "srlinux"
  let ALL_LINKS = [];       // all links from targets
  let TOTAL_BACKBONE = 0;   // set after load

  /* ---------------- tiny DOM helpers ---------------- */
  const $ = (s, r) => (r || document).querySelector(s);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function clock() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  }
  function opt(value, label) {
    const o = document.createElement("option");
    o.value = value; o.textContent = label || value;
    return o;
  }

  /* ---------------- event log ---------------- */
  const logEl = () => $("#log");
  let lineCount = 0;
  function log(level, src, msg) {
    const li = el("li", "evt");
    li.dataset.level = level;
    const tag = level === "error" ? "ALERT" : level === "warn" ? "WARN" : level === "ok" ? "OK" : null;
    li.innerHTML =
      `<time>${clock()}</time>` +
      `<span class="src">${src || ""}</span>` +
      (tag ? `<span class="tag">${tag}</span>` : "") +
      `<span class="msg"></span>`;
    li.querySelector(".msg").textContent = msg;
    logEl().appendChild(li);
    lineCount++;
    $("#logcount").textContent = lineCount + " lines";
    logEl().scrollTop = logEl().scrollHeight;
    return li;
  }
  function clearLog() {
    logEl().innerHTML = "";
    lineCount = 0;
    $("#logcount").textContent = "0 lines";
    log("info", "console", "log cleared");
  }

  /* ---------------- toasts ---------------- */
  const ICONS = {
    danger: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z"/></svg>',
    ok:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>',
    warn:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>',
    info:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
  };
  function toast(kind, title, desc, ttl) {
    ttl = ttl || 4200;
    const t = el("div", "toast " + kind);
    t.innerHTML = `<span class="ic">${ICONS[kind] || ICONS.info}</span><div><div class="tt"></div><div class="td"></div></div>`;
    t.querySelector(".tt").textContent = title;
    t.querySelector(".td").textContent = desc || "";
    $("#toasts").appendChild(t);
    setTimeout(function () {
      t.classList.add("out");
      setTimeout(function () { t.remove(); }, 320);
    }, ttl);
  }

  /* ---------------- status bar (driven entirely by /api/status poll) ---------------- */
  const tiles = {};
  const prevVals = {};

  function flash(key) {
    const t = tiles[key];
    if (!t) return;
    t.classList.remove("flash");
    void t.offsetWidth; // force reflow
    t.classList.add("flash");
  }

  function setTile(key, val, cls) {
    const t = tiles[key];
    if (!t) return;
    const valEl = t.querySelector(".t-val");
    const strVal = String(val);
    if (prevVals[key] !== strVal) {
      prevVals[key] = strVal;
      valEl.textContent = strVal;
      flash(key);
    }
    t.classList.remove("is-alert", "is-warn", "is-busy");
    if (cls) t.classList.add(cls);
  }

  function applyStatus(s) {
    const degraded = s.degraded || [];

    // nodes_up: show X/total
    if (degraded.indexOf("nodes_up") >= 0 || s.nodes_up == null) {
      setTile("nodes", "—", "");
    } else {
      const total = TOTAL_BACKBONE || "?";
      const up = s.nodes_up;
      const display = up + "/" + total;
      const cls = (up < total) ? "is-alert" : "";
      setTile("nodes", display, cls);
    }

    if (degraded.indexOf("links_down") >= 0) {
      setTile("links", "—", "");
    } else {
      setTile("links", s.links_down, s.links_down > 0 ? "is-alert" : "");
    }

    if (degraded.indexOf("alerts_firing") >= 0) {
      setTile("alerts", "—", "");
    } else {
      setTile("alerts", s.alerts_firing, s.alerts_firing > 0 ? "is-alert" : "");
    }

    if (degraded.indexOf("workflows_running") >= 0) {
      setTile("workflows", "—", "");
    } else {
      setTile("workflows", s.workflows_running, s.workflows_running > 2 ? "is-busy" : "");
    }
  }

  async function pollStatus() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const s = await resp.json();
      applyStatus(s);
    } catch (e) {
      // leave tiles as-is; degrade silently
    }
  }

  /* ============================================================
     REAL API CALLS — honest narration only
     ============================================================ */

  // POST to a real endpoint; log cmd + outcome line; return the raw response data.
  async function apiCall(endpoint, body, cmdLine) {
    log("cmd", "atlas", cmdLine);
    try {
      const resp = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(function () { return {}; });
      if (!resp.ok || data.ok === false) {
        const detail = data.detail || ("HTTP " + resp.status);
        log("error", "console", "request failed: " + detail);
        return { ok: false, data };
      }
      const upstream = data.upstream != null ? " (upstream " + data.upstream + ")" : "";
      log("ok", "console", "workflow created" + upstream);
      return { ok: true, data };
    } catch (e) {
      log("error", "console", "request failed: " + e.message);
      return { ok: false, data: {} };
    }
  }

  async function apiCut(node, iface) {
    const r = await apiCall(
      "/api/cut",
      { node, interface: iface, action: "disable" },
      "link disable --node " + node + " --intf " + iface
    );
    if (r.ok) toast("danger", "Cut issued", node + " · " + iface);
    else      toast("warn", "Cut failed", node + " · " + iface);
    return r;
  }

  async function apiRestore(node, iface) {
    const r = await apiCall(
      "/api/cut",
      { node, interface: iface, action: "enable" },
      "link enable --node " + node + " --intf " + iface
    );
    if (r.ok) toast("ok", "Restore issued", node + " · " + iface);
    else      toast("warn", "Restore failed", node + " · " + iface);
    return r;
  }

  async function apiGrayStart(link) {
    const r = await apiCall(
      "/api/gray",
      { link, action: "start" },
      "fault inject --link " + link + " --mode gray"
    );
    if (r.ok) toast("warn", "Gray failure armed", link + " degrading");
    else      toast("warn", "Gray start failed", link);
    return r;
  }

  async function apiGrayEnd(link) {
    const r = await apiCall(
      "/api/gray",
      { link, action: "end" },
      "fault clear --link " + link
    );
    if (r.ok) toast("ok", "Gray failure cleared", link);
    else      toast("warn", "Gray end failed", link);
    return r;
  }

  async function apiMaintOpen(node, hours) {
    const r = await apiCall(
      "/api/maintenance",
      { node, action: "start", hours: Number(hours) },
      "maint open --node " + node + " --hours " + hours
    );
    if (r.ok) toast("info", "Maintenance opened", node + " · " + hours + "h · alerts muted");
    else      toast("warn", "Maintenance failed", node);
    return r;
  }

  async function apiMaintClose(node) {
    const r = await apiCall(
      "/api/maintenance",
      { node, action: "end" },
      "maint close --node " + node
    );
    if (r.ok) toast("ok", "Maintenance closed", node + " back in service");
    else      toast("warn", "Maintenance close failed", node);
    return r;
  }

  /* ============================================================
     SCENARIO RUNNER
     ============================================================ */
  let running = false;
  let cancelToken = 0;
  const alive = (token) => token === cancelToken;

  function setScenBtns(disabled) {
    document.querySelectorAll(".scn").forEach(function (b) { b.disabled = disabled; });
  }

  function runStart(name) {
    if (running) { toast("warn", "Busy", "A scenario is already running"); return null; }
    running = true;
    const token = ++cancelToken;
    $("#runbar").classList.add("on");
    $("#runlabel").textContent = "running scenario: " + name;
    setScenBtns(true);
    return token;
  }

  function runEnd(token, name) {
    if (token !== cancelToken) return;
    running = false;
    $("#runbar").classList.remove("on");
    setScenBtns(false);
    log("ok", "workflow", "scenario \"" + name + "\" complete");
    toast("ok", "Scenario complete", name);
  }

  /*
   * Real scenario target mappings (verified against console-targets.json):
   *
   * hurricane: gray-start ring-e-i20e → cut hub-e ethernet-1/1 → restore hub-e ethernet-1/1 → gray-end ring-e-i20e
   * backhoe:   cut hub-i20e ethernet-1/2 → restore hub-i20e ethernet-1/2
   * cabinet:   maint-open hub-i20e → cut hub-i20e ethernet-1/4 → restore → maint-close hub-i20e
   * flap:      4× cut→restore on tmc-2 ethernet-1/2
   *
   * All node/interface/link names exist in console-targets.json.
   * (ring-e-i20e: hub-e:ethernet-1/1 ↔ hub-i20e:ethernet-1/2)
   * (hubi20e-fci20e cabinet drop: hub-i20e:ethernet-1/4)
   */

  const SCENARIOS = {
    async hurricane(token) {
      log("cmd", "atlas", "scenario hurricane --region coastal-east");
      log("info", "scenario", "hurricane — degrading the I-20 East ring, then cutting hub-e");
      log("info", "scenario", "watch the Geomap / status tiles for the real effect");
      await sleep(900); if (!alive(token)) return;

      log("info", "scenario", "step 1 — gray failure on ring-e-i20e");
      await apiGrayStart("ring-e-i20e");
      await sleep(1100); if (!alive(token)) return;

      log("info", "scenario", "step 2 — cut hub-e ethernet-1/1");
      await apiCut("hub-e", "ethernet-1/1");
      await sleep(1300); if (!alive(token)) return;

      log("info", "scenario", "holding the outage — check Grafana for the fabric response");
      await sleep(1500); if (!alive(token)) return;
      await sleep(1100); if (!alive(token)) return;

      log("info", "scenario", "step 3 — restore hub-e ethernet-1/1");
      await apiRestore("hub-e", "ethernet-1/1");
      await sleep(800); if (!alive(token)) return;

      log("info", "scenario", "step 4 — clear the gray failure on ring-e-i20e");
      await apiGrayEnd("ring-e-i20e");
    },

    async backhoe(token) {
      log("cmd", "atlas", "scenario backhoe --link ring-e-i20e");
      log("info", "scenario", "fiber-cut drill on the I-20 East ring — cut then repair");
      log("info", "scenario", "watch the Geomap / status tiles for the real effect");
      log("info", "scenario", "step 1 — cut hub-i20e ethernet-1/2");
      await apiCut("hub-i20e", "ethernet-1/2");
      await sleep(1200); if (!alive(token)) return;

      log("info", "scenario", "outage in place — check Grafana for the fabric response");
      await sleep(1400); if (!alive(token)) return;
      await sleep(1300); if (!alive(token)) return;

      log("info", "scenario", "simulating splice repair time before restore");
      await sleep(1500); if (!alive(token)) return;
      log("info", "scenario", "step 2 — restore hub-i20e ethernet-1/2");
      await apiRestore("hub-i20e", "ethernet-1/2");
    },

    async cabinet(token) {
      log("cmd", "atlas", "scenario cabinet --node hub-i20e");
      log("info", "scenario", "cabinet drill — maintenance window, then cut the cabinet drop");
      log("info", "scenario", "watch the Geomap / status tiles for the real effect");
      log("info", "scenario", "step 1 — open maintenance on hub-i20e (alerts muted)");
      await apiMaintOpen("hub-i20e", 1);
      await sleep(1200); if (!alive(token)) return;

      await sleep(1300); if (!alive(token)) return;
      log("info", "scenario", "step 2 — cut the cabinet drop hub-i20e ethernet-1/4");
      await apiCut("hub-i20e", "ethernet-1/4");
      await sleep(1500); if (!alive(token)) return;

      log("info", "scenario", "outage in place — check Grafana for the fabric response");
      await sleep(1200); if (!alive(token)) return;
      log("info", "scenario", "step 3 — restore the cabinet drop");
      await apiRestore("hub-i20e", "ethernet-1/4");
      await sleep(800); if (!alive(token)) return;
      log("info", "scenario", "step 4 — close maintenance on hub-i20e");
      await apiMaintClose("hub-i20e");
    },

    async flap(token) {
      log("cmd", "atlas", "scenario flap --node tmc-2 --intf ethernet-1/2");
      log("info", "scenario", "interface flap ×4 on tmc-2 ethernet-1/2");
      log("info", "scenario", "watch the Geomap / status tiles for the real effect");
      for (let i = 1; i <= 4; i++) {
        if (!alive(token)) return;
        log("info", "scenario", "flap " + i + "/4 — cut tmc-2 ethernet-1/2");
        await apiCut("tmc-2", "ethernet-1/2");
        await sleep(700); if (!alive(token)) return;
        log("info", "scenario", "flap " + i + "/4 — restore tmc-2 ethernet-1/2");
        await apiRestore("tmc-2", "ethernet-1/2");
        await sleep(550); if (!alive(token)) return;
      }
      log("info", "scenario", "flap sequence done — check Grafana for the fabric response");
      await sleep(1400); if (!alive(token)) return;
    },
  };

  // Best-effort cleanup when a scenario is aborted mid-run.
  // We don't know what state the scenario left things in, so we just log.
  async function abortCleanup() {
    log("warn", "workflow", "scenario aborted by operator — check fabric state");
    toast("warn", "Aborted", "Scenario stopped — verify fabric manually");
  }

  async function runScenario(name) {
    const token = runStart(name);
    if (token == null) return;
    try {
      await SCENARIOS[name](token);
    } catch (e) {
      log("error", "workflow", "scenario error: " + e.message);
    }
    if (!alive(token)) {
      // was aborted
      return;
    }
    runEnd(token, name);
  }

  /* ============================================================
     SELECT POPULATION + COMMAND PREVIEW
     ============================================================ */
  function populateSelects() {
    const cutNode = $("#cutNode");
    const maintNode = $("#maintNode");
    const grayLink = $("#grayLink");

    BACKBONE_NODES.forEach(function (n) {
      cutNode.appendChild(opt(n.name, n.name + " (" + n.role + ")"));
      maintNode.appendChild(opt(n.name, n.name + " (" + n.role + ")"));
    });

    ALL_LINKS.forEach(function (l) {
      grayLink.appendChild(opt(l.id, l.id + " (" + l.kind + ")"));
    });

    syncInterfaces();
  }

  function syncInterfaces() {
    const node = $("#cutNode").value;
    const sel = $("#cutIntf");
    sel.innerHTML = "";
    const n = BACKBONE_NODES.find(function (x) { return x.name === node; });
    (n ? n.interfaces : []).forEach(function (i) { sel.appendChild(opt(i, i)); });
    updateCmdline();
  }

  function updateCmdline() {
    const node = $("#cutNode").value;
    const iface = $("#cutIntf").value;
    const cmd = $("#cmdpreview");
    if (cmd) {
      cmd.innerHTML =
        'link disable <span class="arg">--node ' + node + '</span>' +
        ' <span class="arg">--intf ' + iface + '</span>';
    }
  }

  /* ============================================================
     ASK THE NETWORK — chat over /api/chat (SSE)
     Read-only Q&A agent; served by the chat-agent Deployment behind
     the same host (ingress path-routes /api/chat). History lives here
     in the browser — the server is stateless.
     ============================================================ */
  const chatHistory = [];      // [{role, content}...] replayed each turn
  let chatBusy = false;

  // Minimal, safe markdown: escape everything, then re-introduce only
  // **bold**, `code`, and "- " bullets. No raw model HTML ever lands.
  function mdLite(text) {
    const esc = String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    const lines = esc.split("\n");
    let html = "", list = false;
    lines.forEach(function (line) {
      const inline = line
        .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
      if (/^\s*[-*] /.test(line)) {
        if (!list) { html += "<ul>"; list = true; }
        html += "<li>" + inline.replace(/^\s*[-*] /, "") + "</li>";
      } else {
        if (list) { html += "</ul>"; list = false; }
        if (line.trim()) html += "<p>" + inline + "</p>";
      }
    });
    if (list) html += "</ul>";
    return html;
  }

  function chatMsg(role, whoLabel) {
    const wrap = el("div", "chat-msg " + role);
    wrap.appendChild(el("div", "who", whoLabel));
    wrap.appendChild(el("div", "body"));
    $("#chatMessages").appendChild(wrap);
    return wrap;
  }

  function chatScroll() {
    const box = $("#chatMessages");
    box.scrollTop = box.scrollHeight;
  }

  function setChatBusy(b) {
    chatBusy = b;
    $("#chatInput").disabled = b;
    $("#chatSend").disabled = b;
    document.querySelectorAll("#chatChips .chip").forEach(function (c) {
      c.disabled = b;
    });
  }

  async function chatAsk(question) {
    if (chatBusy || !question.trim()) return;
    $("#chatModule").querySelector(".chat-empty")?.remove();
    setChatBusy(true);
    const userEl = chatMsg("user", "you");
    userEl.querySelector(".body").textContent = question;
    chatHistory.push({ role: "user", content: question });

    const botEl = chatMsg("assistant", "network assistant");
    const body = botEl.querySelector(".body");
    const trace = el("div", "chat-trace");
    botEl.insertBefore(trace, body);
    body.innerHTML = '<span class="chat-thinking">thinking</span>';
    chatScroll();

    let answer = "", gotDone = false;
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: chatHistory.slice(-21) }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(function () { return {}; });
        throw new Error(data.detail || ("HTTP " + resp.status));
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          let ev = null, data = {};
          frame.split("\n").forEach(function (line) {
            if (line.startsWith("event: ")) ev = line.slice(7);
            else if (line.startsWith("data: ")) {
              try { data = JSON.parse(line.slice(6)); } catch (e) {}
            }
          });
          if (ev === "tool") {
            const t = el("span", "t-line");
            t.innerHTML = '<span class="t-name"></span> <span class="t-args"></span>';
            t.querySelector(".t-name").textContent = data.name || "tool";
            t.querySelector(".t-args").textContent = data.summary || "";
            trace.appendChild(t);
            log("info", "chat", "tool call: " + (data.name || "?"));
          } else if (ev === "token") {
            answer += data.text || "";
            body.innerHTML = mdLite(answer);
          } else if (ev === "done") {
            answer = data.text || answer;
            gotDone = true;
          } else if (ev === "error") {
            throw new Error(data.detail || "assistant error");
          }
          chatScroll();
        }
      }
      if (!answer) throw new Error("the assistant returned no answer");
      body.innerHTML = mdLite(answer);
      chatHistory.push({ role: "assistant", content: answer });
      if (!gotDone) log("warn", "chat", "answer stream ended without done event");
    } catch (e) {
      chatHistory.pop(); // failed turn doesn't poison the next one
      botEl.classList.add("error");
      trace.remove();
      body.textContent = e.message;
      log("error", "chat", e.message);
    }
    setChatBusy(false);
    chatScroll();
  }

  async function chatInit() {
    const box = $("#chatMessages");
    box.appendChild(el("div", "chat-empty",
      "ask about topology, blast radius, alerts, or inventory — answers " +
      "come from NetBox, Prometheus and Loki"));
    try {
      const s = await fetch("/api/chat/status").then(function (r) { return r.json(); });
      if (s.enabled) {
        log("ok", "chat", "network assistant online" + (s.model ? " · " + s.model : ""));
      } else {
        throw new Error("disabled");
      }
    } catch (e) {
      $("#chatModule").classList.add("chat-off");
      $("#chatHint").textContent = "disabled";
      $("#chatInput").disabled = true;
      $("#chatSend").disabled = true;
      document.querySelectorAll("#chatChips .chip").forEach(function (c) { c.disabled = true; });
      box.innerHTML = "";
      box.appendChild(el("div", "chat-empty",
        "AI chat is off — mount the ai-analyst Secret to enable it (SECRETS.md)"));
      return;
    }
    $("#chatForm").addEventListener("submit", function (ev) {
      ev.preventDefault();
      const q = $("#chatInput").value;
      $("#chatInput").value = "";
      chatAsk(q);
    });
    document.querySelectorAll("#chatChips .chip").forEach(function (c) {
      c.addEventListener("click", function () { chatAsk(c.dataset.q); });
    });
  }

  /* ============================================================
     VIEW + THEME PERSISTENCE
     ============================================================ */
  const STORE = "atlas-console-prefs";
  function loadPrefs() {
    let p = {};
    try { p = JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) {}
    const url = new URLSearchParams(location.search);
    const view = url.get("view") || p.view || "mission";
    const theme = url.get("theme") || p.theme || "dark";
    applyView(view); applyTheme(theme);
  }
  function savePrefs() {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        view: document.body.dataset.view,
        theme: document.body.dataset.theme,
      }));
    } catch (e) {}
  }
  function applyView(v) {
    document.body.dataset.view = v;
    document.querySelectorAll("[data-set-view]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.setView === v ? "true" : "false");
    });
    updateCmdline();
  }
  function applyTheme(t) {
    document.body.dataset.theme = t;
    document.querySelectorAll("[data-set-theme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.setTheme === t ? "true" : "false");
    });
  }

  /* ============================================================
     BOOT
     ============================================================ */
  document.addEventListener("DOMContentLoaded", async function () {
    // Cache tile references
    ["nodes", "links", "alerts", "workflows"].forEach(function (k) {
      tiles[k] = $("#tile-" + k);
    });

    // Load inventory
    try {
      const targets = await fetch("console-targets.json").then(function (r) { return r.json(); });
      BACKBONE_NODES = (targets.nodes || []).filter(function (n) { return n.kind === "srlinux"; });
      ALL_LINKS = targets.links || [];
      TOTAL_BACKBONE = BACKBONE_NODES.length;
    } catch (e) {
      log("error", "console", "failed to load inventory: " + e.message);
    }

    populateSelects();
    loadPrefs();

    // Boot log
    log("info", "console", "Atlas DOT scenario console v2 — connected to lab fabric");
    log("ok", "console", TOTAL_BACKBONE + " backbone nodes in inventory · " + ALL_LINKS.length + " links monitored");

    // Start status poll
    await pollStatus();
    setInterval(pollStatus, 5000);

    // Ask-the-network chat (self-disables when the AI Secret is absent)
    chatInit();

    // Cut / restore
    $("#cutNode").addEventListener("change", syncInterfaces);
    $("#cutIntf").addEventListener("change", updateCmdline);

    $("#btnCut").addEventListener("click", function () {
      const node = $("#cutNode").value;
      const iface = $("#cutIntf").value;
      if (!node || !iface) return;
      apiCut(node, iface);
    });
    $("#btnRestore").addEventListener("click", function () {
      const node = $("#cutNode").value;
      const iface = $("#cutIntf").value;
      if (!node || !iface) return;
      apiRestore(node, iface);
    });

    // Gray failure
    $("#btnGrayStart").addEventListener("click", function () {
      const link = $("#grayLink").value;
      if (!link) return;
      apiGrayStart(link);
    });
    $("#btnGrayEnd").addEventListener("click", function () {
      const link = $("#grayLink").value;
      if (!link) return;
      apiGrayEnd(link);
    });

    // Maintenance
    $("#btnMaintOpen").addEventListener("click", function () {
      const node = $("#maintNode").value;
      const hours = $("#maintHours").value || 2;
      if (!node) return;
      apiMaintOpen(node, hours);
    });
    $("#btnMaintClose").addEventListener("click", function () {
      const node = $("#maintNode").value;
      if (!node) return;
      apiMaintClose(node);
    });

    // Scenarios
    document.querySelectorAll(".scn").forEach(function (b) {
      b.addEventListener("click", function () { runScenario(b.dataset.scn); });
    });

    // Abort
    $("#stopRun").addEventListener("click", function () {
      if (!running) return;
      cancelToken++;
      running = false;
      $("#runbar").classList.remove("on");
      setScenBtns(false);
      abortCleanup();
    });

    // Clear log
    $("#clearLog").addEventListener("click", clearLog);

    // View/theme switchers
    document.querySelectorAll("[data-set-view]").forEach(function (b) {
      b.addEventListener("click", function () { applyView(b.dataset.setView); savePrefs(); });
    });
    document.querySelectorAll("[data-set-theme]").forEach(function (b) {
      b.addEventListener("click", function () { applyTheme(b.dataset.setTheme); savePrefs(); });
    });
  });
})();
