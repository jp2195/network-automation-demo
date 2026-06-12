"""Read-only tool functions exposed to the AI incident analyst.

Every tool validates its arguments against an allowlist BEFORE touching
the network, so a hallucinating or prompt-injected model cannot aim the
clients anywhere unexpected: gNMI only at SRL nodes (tmc-*/hub-*), SNMP
only at FRR cabinets (fc-*), NetBox only at GET /api/... paths. Bad
arguments raise ModelRetry (the model gets a correctable error); network
failures return {"error": ...} so one dead endpoint doesn't abort the
whole analysis. Docstrings below are the tool descriptions the model
sees — keep them instructive.

Heavy deps (puresnmp; pygnmi via gnmi_readonly) import lazily inside the
accept path so the unit tests run with pydantic-ai-slim alone.
"""

import asyncio
import os
import re
import time

from pydantic_ai import ModelRetry

import gnmi_readonly
from constants import CABINET_NAME_PREFIX
from loki import loki_query_range
from prom import prom_query, prom_query_range

_SRL_NODE_RE = re.compile(r"^(tmc|hub)-[a-z0-9-]{1,28}$")
_FRR_NODE_RE = re.compile(r"^" + CABINET_NAME_PREFIX + r"[a-z0-9-]{1,28}$")
_OID_RE = re.compile(r"^\.?\d+(\.\d+)+$")
# GET-only REST paths under /api/ — no query strings (params go through
# urlencode separately), no traversal, no absolute URLs.
_NETBOX_PATH_RE = re.compile(r"^/api/[a-z0-9_-]+(/[a-z0-9_-]+)*/?$")


def _clamp_minutes(minutes):
    try:
        return max(1, min(int(minutes), 360))
    except (TypeError, ValueError):
        return 30


def query_prometheus(promql: str) -> list:
    """Run an instant PromQL query against the network's Prometheus.

    Useful series: link:oper_state_with_meta{link_id="..."} (1=up, 2=down,
    labels: node/interface/corridor), link_membership_info{link_id="..."}
    (both ends of a link), srl_nokia_interfaces_interface_oper_state,
    ALERTS{alertstate="firing"} (everything currently firing).
    Returns the Prometheus result list (empty list on error/no data)."""
    return prom_query(os.environ["PROM_URL"], promql)


# A 360-minute window at 30s step is 720 points per series; an
# unfiltered query could match hundreds of series and blow the model's
# context. 20 series is plenty at this topology's scale (8 SRL + 4 FRR).
_MAX_RANGE_SERIES = 20


def query_prometheus_range(promql: str, minutes: int = 30) -> list:
    """Run a PromQL range query over the last `minutes` (max 360),
    30s step. Use label filters — at most 20 series are returned.
    Returns the Prometheus matrix result list (empty on error)."""
    now = time.time()
    return prom_query_range(os.environ["PROM_URL"], promql,
                            now - _clamp_minutes(minutes) * 60, now,
                            step=30)[:_MAX_RANGE_SERIES]


def query_loki(logql: str, minutes: int = 30) -> list:
    """Search logs in Loki over the last `minutes` (max 360).

    Device logs: {namespace="clabernetes"} |= "hub-e". Workflow logs:
    {namespace="argo-events"}. Returns up to 100 [timestamp_ns, line]
    pairs, oldest first (empty on error/no match)."""
    now = time.time()
    rows = loki_query_range(os.environ["LOKI_URL"], logql,
                            now - _clamp_minutes(minutes) * 60, now, limit=100)
    return [[ts, line[:500]] for ts, line in rows]


def query_netbox(path: str, params: dict | None = None) -> dict:
    """GET a NetBox REST API path — the network source of truth.

    Examples: query_netbox("/api/dcim/devices/", {"name": "hub-e"}),
    query_netbox("/api/dcim/cables/", {"label": "FOC-RING-EI20E"}).
    Device tags carry the agencies a cabinet serves. Returns the JSON
    body ({"error": ...} on HTTP failure)."""
    if not _NETBOX_PATH_RE.fullmatch(path or ""):
        raise ModelRetry(
            "path must be a NetBox REST path like /api/dcim/devices/ "
            "(no query string — pass filters via params)")
    from netbox_client import Client
    try:
        return Client().get(path, **(params or {}))
    except Exception as e:
        return {"error": str(e)}


def gnmi_get(node: str, path: str) -> dict:
    """gNMI Get against a live SR Linux node (names tmc-* or hub-*).

    Examples: gnmi_get("hub-e", "/interface[name=ethernet-1/1]"),
    gnmi_get("hub-e", "/network-instance[name=default]/protocols/"
    "srl_nokia-isis:isis/instance[name=atlas]") for IS-IS adjacencies.
    Returns decoded json_ietf notifications ({"error": ...} if the
    device is unreachable)."""
    if not _SRL_NODE_RE.fullmatch(node or ""):
        raise ModelRetry("node must be an SR Linux node: tmc-* or hub-* "
                         "(fc-* cabinets speak SNMP — use snmp_get)")
    try:
        out = gnmi_readonly.get(node, path)
    except ValueError as e:
        raise ModelRetry(str(e))
    except Exception as e:
        return {"error": str(e)}
    # pygnmi yields None for an empty notification; a None tool return
    # serializes to a null tool message, which some OpenAI-compatible
    # servers (Ollama) reject with 400 invalid message content.
    return out if out is not None else {"error": "empty gNMI response"}


async def snmp_get(node: str, oid: str) -> dict:
    """SNMPv2c GET against a legacy FRR field cabinet (names fc-*).

    Numeric OIDs only. Examples: 1.3.6.1.2.1.1.3.0 (sysUpTime),
    1.3.6.1.2.1.2.2.1.8.<ifIndex> (ifOperStatus: 1=up, 2=down),
    1.3.6.1.2.1.31.1.1.1.1.<ifIndex> (ifName). Returns
    {"oid": ..., "value": ...} ({"error": ...} if unreachable)."""
    if not _FRR_NODE_RE.fullmatch(node or ""):
        raise ModelRetry("node must be an FRR field cabinet: fc-* "
                         "(SRL nodes speak gNMI — use gnmi_get)")
    if not _OID_RE.fullmatch(oid or ""):
        raise ModelRetry("oid must be numeric dotted form, e.g. 1.3.6.1.2.1.1.3.0")
    try:
        from puresnmp import V2C, Client as SnmpClient, PyWrapper
        host = (f"{os.environ['CLAB_PREFIX']}-{node}"
                ".clabernetes.svc.cluster.local")
        client = PyWrapper(SnmpClient(
            host, V2C(os.environ.get("SNMP_COMMUNITY", "public"))))
        value = await asyncio.wait_for(client.get(oid.lstrip(".")), timeout=10)
        return {"oid": oid, "value": str(value)}
    except Exception as e:
        return {"error": str(e)}


ALL_TOOLS = [query_prometheus, query_prometheus_range, query_loki,
             query_netbox, gnmi_get, snmp_get]
