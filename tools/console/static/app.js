async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, data: await r.json().catch(() => ({})) };
}
function opt(value, label) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label || value;
  return o;
}
function show(id, ok, msg) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.className = "result " + (ok ? "ok" : "err");
}

let TARGETS = { nodes: [], links: [] };

async function init() {
  TARGETS = await getJSON("console-targets.json");
  const cutNode = document.getElementById("cut-node");
  const maintNode = document.getElementById("maint-node");
  TARGETS.nodes.forEach((n) => {
    cutNode.appendChild(opt(n.name, `${n.name} (${n.role})`));
    maintNode.appendChild(opt(n.name, `${n.name} (${n.role})`));
  });
  document.getElementById("gray-link").append(
    ...TARGETS.links.map((l) => opt(l.id, `${l.id} (${l.kind})`)));
  syncIfaces();
  cutNode.addEventListener("change", syncIfaces);

  bind("cut", async (act) => postJSON("/api/cut", {
    node: cutNode.value,
    interface: document.getElementById("cut-iface").value,
    action: act,
  }));
  bind("gray", async (act) => postJSON("/api/gray", {
    link: document.getElementById("gray-link").value,
    action: act,
  }));
  bind("maint", async (act) => postJSON("/api/maintenance", {
    node: maintNode.value,
    hours: document.getElementById("maint-hours").value,
    action: act,
  }));

  refresh();
  setInterval(refresh, 5000);
}

function syncIfaces() {
  const node = document.getElementById("cut-node").value;
  const sel = document.getElementById("cut-iface");
  sel.innerHTML = "";
  const n = TARGETS.nodes.find((x) => x.name === node);
  (n ? n.interfaces : []).forEach((i) => sel.appendChild(opt(i)));
}

function bind(prefix, fn) {
  const card = document.getElementById(`${prefix}-result`).closest(".card");
  card.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { ok, data } = await fn(btn.dataset.act);
      show(`${prefix}-result`, ok && data.ok !== false,
        ok ? JSON.stringify(data) : "request failed");
    });
  });
}

async function refresh() {
  try {
    const s = await getJSON("/api/status");
    const parts = [
      `backbone nodes up: ${fmt(s.nodes_up)}`,
      `links down: ${fmt(s.links_down)}`,
      `alerts firing: ${fmt(s.alerts_firing)}`,
      `workflows running: ${fmt(s.workflows_running)}`,
    ];
    let strip = parts.join(" · ");
    if (s.degraded && s.degraded.length) strip += `  (no data: ${s.degraded.join(", ")})`;
    document.getElementById("strip").textContent = strip;
  } catch (e) {
    document.getElementById("strip").textContent = "status unavailable";
  }
}
function fmt(v) { return v === undefined ? "—" : v; }

init();
