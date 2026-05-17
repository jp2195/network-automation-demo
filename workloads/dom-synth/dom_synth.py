#!/usr/bin/env python3
"""Synthetic equipment metrics exporter for the demo.

Three families of metrics, all driven by /data/links.json:

  Optical (per backbone+cabinet port on SR Linux):
    dom_temperature_celsius       transceiver case temp
    dom_rx_power_dbm              optical receive power
    dom_tx_power_dbm              optical transmit power
    dom_voltage_volts             transceiver supply voltage
    dom_bias_current_milliamps    laser bias current

  Hardware (per spec node):
    chassis_temperature_celsius   per chassis temp sensor
    fan_speed_rpm                 per fan
    psu_input_voltage_volts       per PSU
    psu_state                     1=online, 0=offline (synthetic, all 1)

  Routing protocol (one row per spec adjacency / peer):
    isis_adjacency_info           constant 1, labeled with neighbor / sysid
    isis_adjacency_uptime_seconds synthetic monotonic uptime
    bgp_peer_info                 constant 1, labeled with peer / asn / group
    bgp_peer_uptime_seconds       synthetic monotonic uptime
    bgp_peer_prefixes_received    synthetic, depends on peer group

Dashboards then join these against live oper-state metrics
(`srl_nokia_interfaces_interface_oper_state`, `up{job="snmp-frr-cabinets"}`)
so a downed link / unreachable cabinet correctly shows the protocol
session as down.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import math
import os
import socketserver
import time
from dataclasses import dataclass
from typing import Optional


LINKS_FILE = os.environ.get("LINKS_FILE", "/data/links.json")
PORT = int(os.environ.get("PORT", "8000"))
DATA: dict = {}
START_TIME = time.time()
VALKEY_URL = os.environ.get("VALKEY_URL", "valkey://valkey.valkey.svc.cluster.local:6379/3")
LOG = logging.getLogger("dom-synth")

# Cumulative synthetic counter state, keyed by (node, interface).
SYNTH_ERRORS_TOTAL: dict[tuple[str, str], float] = {}
SYNTH_DISCARDS_TOTAL: dict[tuple[str, str], float] = {}
LAST_SYNTH_TICK = time.time()

# Rate-limit Valkey-unreachable warnings to once per minute.
_LAST_VALKEY_WARN = 0.0


@dataclass(frozen=True)
class GrayFailure:
    link_id: str
    start_ts: float
    duration_s: float
    peak_rx_offset_dbm: float
    peak_errors_per_sec: float


def ramp(t_now: float, gf: GrayFailure) -> float:
    """Piecewise-linear shape with 20% ramp-up, 60% plateau, 20% ramp-down."""
    if gf.duration_s <= 0:
        return 0.0
    rel = (t_now - gf.start_ts) / gf.duration_s
    if rel <= 0.0 or rel >= 1.0:
        return 0.0
    if rel < 0.20:
        return rel / 0.20
    if rel < 0.80:
        return 1.0
    return (1.0 - rel) / 0.20


def parse_gray_failure(link_id: str, raw) -> Optional[GrayFailure]:
    """Parse a Valkey value into a GrayFailure. Returns None on any error."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    required = ("start_ts", "duration_s", "peak_rx_offset_dbm", "peak_errors_per_sec")
    if not all(k in d for k in required):
        return None
    try:
        duration = float(d["duration_s"])
        if duration <= 0:
            return None
        return GrayFailure(
            link_id=link_id,
            start_ts=float(d["start_ts"]),
            duration_s=duration,
            peak_rx_offset_dbm=float(d["peak_rx_offset_dbm"]),
            peak_errors_per_sec=float(d["peak_errors_per_sec"]),
        )
    except (TypeError, ValueError):
        return None


def _load_gray_failures(client=None) -> dict[str, GrayFailure]:
    """Read all `gray:*` keys from Valkey DB 3 and parse them.

    The `client` argument is for tests; production callers pass None and
    we lazily build a real redis.Redis from VALKEY_URL.
    """
    global _LAST_VALKEY_WARN
    if client is None:
        try:
            import valkey
            client = valkey.from_url(VALKEY_URL, socket_timeout=2)
        except Exception:
            return {}
    out: dict[str, GrayFailure] = {}
    try:
        keys = client.keys("gray:*")
    except Exception as exc:
        now = time.time()
        if now - _LAST_VALKEY_WARN > 60:
            LOG.warning("Valkey unreachable for gray-failure poll: %s", exc)
            _LAST_VALKEY_WARN = now
        return out
    for k in keys:
        key_str = k.decode("utf-8") if isinstance(k, bytes) else k
        link_id = key_str.removeprefix("gray:")
        try:
            raw = client.get(k)
        except Exception:
            continue
        gf = parse_gray_failure(link_id, raw)
        if gf is not None:
            out[link_id] = gf
    return out


def load_data() -> dict:
    with open(LINKS_FILE) as f:
        return json.load(f)


def offset(*parts: str) -> float:
    """Stable per-port offset in [0, 2π)."""
    h = hashlib.md5("|".join(parts).encode()).hexdigest()
    return (int(h[:8], 16) % 1000) / 1000.0 * 2.0 * math.pi


def render_metrics() -> str:
    now = time.time()
    out: list[str] = []

    def hdr(name: str, help_: str, kind: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")

    # ── Optical / DOM ─────────────────────────────────────────────────
    ports = DATA.get("ports", [])

    hdr("dom_temperature_celsius", "Transceiver case temperature (synthetic)")
    for l in ports:
        o = offset(l["node"], l["interface"])
        v = 38.0 + 6.0 * math.sin((now / 120.0) + o)
        out.append(_metric("dom_temperature_celsius", l, v))
    hdr("dom_rx_power_dbm", "Optical receive power (synthetic)")
    for l in ports:
        o = offset(l["node"], l["interface"])
        v = -4.5 + 1.5 * math.sin((now / 90.0) + o + 0.7)
        out.append(_metric("dom_rx_power_dbm", l, v))
    hdr("dom_tx_power_dbm", "Optical transmit power (synthetic)")
    for l in ports:
        o = offset(l["node"], l["interface"])
        v = 0.5 + 1.0 * math.sin((now / 60.0) + o + 1.2)
        out.append(_metric("dom_tx_power_dbm", l, v))
    hdr("dom_voltage_volts", "Transceiver supply voltage (synthetic)")
    for l in ports:
        o = offset(l["node"], l["interface"])
        v = 3.30 + 0.05 * math.sin((now / 240.0) + o)
        out.append(_metric("dom_voltage_volts", l, v))
    hdr("dom_bias_current_milliamps", "Laser bias current (synthetic)")
    for l in ports:
        o = offset(l["node"], l["interface"])
        v = 32.0 + 4.0 * math.sin((now / 180.0) + o + 2.1)
        out.append(_metric("dom_bias_current_milliamps", l, v))

    # ── Hardware health ──────────────────────────────────────────────
    nodes = DATA.get("nodes", [])

    hdr("chassis_temperature_celsius", "Chassis sensor temperature (synthetic)")
    for n in nodes:
        for tid in n.get("temp_ids", []):
            o = offset(n["node"], "temp", tid)
            base = {"intake": 24.0, "exhaust": 38.0, "linecard": 45.0, "cpu": 52.0, "ambient": 22.0}.get(tid, 30.0)
            v = base + 3.0 * math.sin((now / 150.0) + o)
            out.append(f'chassis_temperature_celsius{{node="{n["node"]}",sensor="{tid}",chassis="{n["chassis"]}"}} {v:.2f}')

    hdr("fan_speed_rpm", "Fan tray speed in RPM (synthetic)")
    for n in nodes:
        for fid in n.get("fan_ids", []):
            o = offset(n["node"], "fan", fid)
            v = 8200.0 + 600.0 * math.sin((now / 90.0) + o)
            out.append(f'fan_speed_rpm{{node="{n["node"]}",fan="{fid}",chassis="{n["chassis"]}"}} {v:.0f}')

    hdr("psu_input_voltage_volts", "PSU input voltage (synthetic)")
    for n in nodes:
        for pid in n.get("psu_ids", []):
            o = offset(n["node"], "psu", pid)
            v = 207.0 + 4.0 * math.sin((now / 220.0) + o)
            out.append(f'psu_input_voltage_volts{{node="{n["node"]}",psu="{pid}",chassis="{n["chassis"]}"}} {v:.1f}')

    hdr("psu_state", "PSU operational state (1=online, 0=offline)")
    for n in nodes:
        for pid in n.get("psu_ids", []):
            out.append(f'psu_state{{node="{n["node"]}",psu="{pid}",chassis="{n["chassis"]}"}} 1')

    # ── Routing protocol ─────────────────────────────────────────────
    isis = DATA.get("isis_adjacencies", [])
    bgp = DATA.get("bgp_peers", [])
    uptime = max(0.0, now - START_TIME)

    hdr("isis_adjacency_info", "Constant 1 per spec adjacency (join with oper-state)")
    for a in isis:
        out.append(
            f'isis_adjacency_info{{node="{a["node"]}",interface="{a["interface"]}",'
            f'neighbor="{a["neighbor"]}",system_id="{a["system_id"]}",'
            f'link_id="{a["link_id"]}",level="{a["level"]}"}} 1'
        )
    hdr("isis_adjacency_uptime_seconds", "Synthetic monotonic adjacency uptime")
    for a in isis:
        out.append(
            f'isis_adjacency_uptime_seconds{{node="{a["node"]}",interface="{a["interface"]}",'
            f'neighbor="{a["neighbor"]}",link_id="{a["link_id"]}"}} {uptime:.0f}'
        )

    hdr("bgp_peer_info", "Constant 1 per spec BGP peer (join with reachability)")
    for p in bgp:
        out.append(
            f'bgp_peer_info{{node="{p["node"]}",neighbor_node="{p["neighbor_node"]}",'
            f'peer_address="{p["peer_address"]}",peer_as="{p["peer_as"]}",'
            f'peer_group="{p["peer_group"]}"}} 1'
        )
    hdr("bgp_peer_uptime_seconds", "Synthetic monotonic BGP peer uptime")
    for p in bgp:
        out.append(
            f'bgp_peer_uptime_seconds{{node="{p["node"]}",neighbor_node="{p["neighbor_node"]}",'
            f'peer_group="{p["peer_group"]}"}} {uptime:.0f}'
        )
    hdr("bgp_peer_prefixes_received", "Synthetic BGP prefix count per peer")
    for p in bgp:
        # cabinet eBGP advertises ~1-3 prefixes. iBGP TMC mesh: a couple
        # of fixed loopbacks plus learned.
        if p["peer_group"] == "cabinets":
            count = 1
        elif p["peer_group"] == "uplink":
            count = 8
        elif p["peer_group"] == "tmc-ibgp":
            count = 2
        else:
            count = 0
        out.append(
            f'bgp_peer_prefixes_received{{node="{p["node"]}",neighbor_node="{p["neighbor_node"]}",'
            f'peer_group="{p["peer_group"]}"}} {count}'
        )

    out.append("")
    return "\n".join(out)


def _metric(name: str, l: dict, value: float) -> str:
    parts = [f'{k}="{l[k]}"' for k in ("node", "interface", "link_id", "link_kind")]
    return f"{name}{{{','.join(parts)}}} {value:.4f}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):
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
    global DATA
    DATA = load_data()
    print(
        f"dom-synth: ports={len(DATA.get('ports',[]))} "
        f"nodes={len(DATA.get('nodes',[]))} "
        f"isis={len(DATA.get('isis_adjacencies',[]))} "
        f"bgp={len(DATA.get('bgp_peers',[]))}",
        flush=True,
    )
    print(f"dom-synth: listening on :{PORT}", flush=True)
    ThreadingServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
