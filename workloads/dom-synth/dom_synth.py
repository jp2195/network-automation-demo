#!/usr/bin/env python3
"""Synthetic DOM (digital optical monitoring) exporter for the demo.

clabernetes SR Linux has no real SFPs, so the transceiver YANG paths
emit zeros. This script fakes realistic-looking transceiver metrics —
case temperature, rx/tx optical power, supply voltage and bias current
— per backbone interface defined in /data/links.json. Values wobble
with a sine wave plus a per-port offset so charts look alive.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import math
import os
import socketserver
import sys
import threading
import time


LINKS_FILE = os.environ.get("LINKS_FILE", "/data/links.json")
PORT = int(os.environ.get("PORT", "8000"))
LINKS: list[dict] = []


def load_links() -> list[dict]:
    with open(LINKS_FILE) as f:
        return json.load(f)


def offset(node: str, interface: str) -> float:
    """Stable per-port offset in [0, 2π)."""
    h = hashlib.md5(f"{node}|{interface}".encode()).hexdigest()
    return (int(h[:8], 16) % 1000) / 1000.0 * 2.0 * math.pi


def render_metrics() -> str:
    now = time.time()
    out: list[str] = []

    def block(name: str, help_: str, unit: str, kind: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")

    block("dom_temperature_celsius", "Transceiver case temperature (synthetic)", "C")
    for l in LINKS:
        o = offset(l["node"], l["interface"])
        # Range 32-44C: 38 baseline + 6 amplitude over a 2-min cycle.
        v = 38.0 + 6.0 * math.sin((now / 120.0) + o)
        out.append(_metric("dom_temperature_celsius", l, v))

    block("dom_rx_power_dbm", "Transceiver optical receive power (synthetic)", "dBm")
    for l in LINKS:
        o = offset(l["node"], l["interface"])
        # Range -6 to -3 dBm: -4.5 baseline, 1.5 amplitude over a 90s cycle.
        v = -4.5 + 1.5 * math.sin((now / 90.0) + o + 0.7)
        out.append(_metric("dom_rx_power_dbm", l, v))

    block("dom_tx_power_dbm", "Transceiver optical transmit power (synthetic)", "dBm")
    for l in LINKS:
        o = offset(l["node"], l["interface"])
        # Range -1 to +2 dBm: 0.5 baseline, 1.0 amplitude over a 60s cycle.
        v = 0.5 + 1.0 * math.sin((now / 60.0) + o + 1.2)
        out.append(_metric("dom_tx_power_dbm", l, v))

    block("dom_voltage_volts", "Transceiver supply voltage (synthetic)", "V")
    for l in LINKS:
        o = offset(l["node"], l["interface"])
        v = 3.30 + 0.05 * math.sin((now / 240.0) + o)
        out.append(_metric("dom_voltage_volts", l, v))

    block("dom_bias_current_milliamps", "Transceiver laser bias current (synthetic)", "mA")
    for l in LINKS:
        o = offset(l["node"], l["interface"])
        v = 32.0 + 4.0 * math.sin((now / 180.0) + o + 2.1)
        out.append(_metric("dom_bias_current_milliamps", l, v))

    out.append("")
    return "\n".join(out)


def _metric(name: str, l: dict, value: float) -> str:
    parts = [f'{k}="{l[k]}"' for k in ("node", "interface", "link_id", "link_kind")]
    return f"{name}{{{','.join(parts)}}} {value:.4f}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global LINKS
    LINKS = load_links()
    print(f"dom-synth: loaded {len(LINKS)} interfaces from {LINKS_FILE}", flush=True)
    print(f"dom-synth: listening on :{PORT}", flush=True)
    ThreadingServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
