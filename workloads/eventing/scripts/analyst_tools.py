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
import json
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


def _parse_iso_epoch(s):
    """Best-effort ISO8601 -> epoch seconds (Alertmanager `startsAt`),
    or None. Tolerates a trailing Z and 9-digit nanoseconds (Alertmanager
    emits both; datetime.fromisoformat caps at microseconds)."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = re.sub(r"\.(\d{6})\d+", r".\1", s.strip().replace("Z", "+00:00"))
    try:
        from datetime import datetime
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


# Every tool result is byte-bounded before it reaches the model: one
# un-filtered gNMI subtree or NetBox list can be tens of thousands of
# tokens, which floods a small local model's context window and makes
# it forget its own earlier tool results (smoke-found: the agent then
# wanders until the request limit). 5000 chars ≈ 1.2k tokens.
_MAX_RESULT_CHARS = 5000


# Anti-repeat guard: at low temperature a small model that repeats one
# query verbatim will repeat it forever (smoke-found: 23 identical
# range queries until the request limit). Two identical calls are
# legitimate (re-check after another tool); the third gets a corrective
# ModelRetry instead of burning the budget.
_seen_calls = {}


def _repeat_guard(tool, key):
    n = _seen_calls.get((tool, key), 0) + 1
    _seen_calls[(tool, key)] = n
    if n > 2:
        raise ModelRetry(
            f"you already called {tool} with these exact arguments "
            f"{n - 1} times — the result will not change. Try a "
            f"different query or tool, or call submit_incident_analysis "
            f"with what you have.")


def _bounded(result):
    s = json.dumps(result, separators=(",", ":"), default=str)
    if len(s) <= _MAX_RESULT_CHARS:
        return result
    return {"truncated": True,
            "note": (f"result was {len(s)} chars; first "
                     f"{_MAX_RESULT_CHARS} shown — repeat the call with "
                     f"a narrower filter/path for full detail"),
            "head": s[:_MAX_RESULT_CHARS]}


def query_prometheus(promql: str) -> list:
    """Run an instant PromQL query against the network's Prometheus.

    Useful series: link:oper_state_with_meta{link_id="..."} (1=up, 2=down,
    labels: node/interface/corridor), link_membership_info{link_id="..."}
    (both ends of a link), srl_nokia_interfaces_interface_oper_state,
    ALERTS{alertstate="firing"} (everything currently firing).
    Returns the Prometheus result list (empty list on error/no data)."""
    _repeat_guard("query_prometheus", promql)
    return _bounded(prom_query(os.environ["PROM_URL"], promql))


# A 360-minute window at 30s step is 720 points per series; an
# unfiltered query could match hundreds of series and blow the model's
# context. 20 series is plenty at this topology's scale (8 SRL + 4 FRR).
_MAX_RANGE_SERIES = 20


def query_prometheus_range(promql: str, minutes: int = 30) -> list:
    """Run a PromQL range query over the last `minutes` (max 360),
    30s step. Use label filters — at most 20 series are returned.
    Returns the Prometheus matrix result list (empty on error)."""
    # Models habitually append a range selector ([5m]) — the range API
    # rejects it (window comes from `minutes`), so strip rather than
    # return a confusing [].
    promql = re.sub(r"\[\d+[smhdw]\]\s*$", "", (promql or "").strip())
    _repeat_guard("query_prometheus_range", (promql, _clamp_minutes(minutes)))
    now = time.time()
    return _bounded(prom_query_range(os.environ["PROM_URL"], promql,
                                     now - _clamp_minutes(minutes) * 60, now,
                                     step=30)[:_MAX_RANGE_SERIES])


def query_loki(logql: str, minutes: int = 30, around: str | None = None) -> list:
    """Search logs in Loki. By default the trailing `minutes` (max 360,
    ending now). Pass `around` — an ISO8601 instant, e.g. the alert's
    `startsAt` — to instead search a window of `minutes` total *centered*
    on that moment. For an outage this is the move: bracket the fault and
    read what changed just before it, rather than scrolling from now.

    Streams worth knowing:
      {namespace="clabernetes"} |= "hub-e"        device pod stdout
      {namespace="argo-events"}                    workflow logs
      {source_type="syslog", host="hub-e"}         SR Linux syslog, per node
    Config changes name the operator in the SR Linux AAA syslog:
    sr_mgmt_server logs `committed successfully by user <u> session <n>`
    (the change + who), and sr_aaa_mgr logs `session <n> for user <u> from
    host <ip>` (the source). A gNMI-originated change — the usual case here
    — has NO per-command sr_cli line, so the committed-by-user line IS the
    attribution; one match answers WHO. Query
      {source_type="syslog", host="<node>"} |~ "committed successfully by user"
    with around=<alert startsAt>. Returns up to 100 [timestamp_ns, line]
    pairs, oldest first (empty on error/no match)."""
    span = _clamp_minutes(minutes)
    _repeat_guard("query_loki", (logql, span, (around or "").strip()))
    center = _parse_iso_epoch(around)
    if center is not None:
        half = span * 60 / 2
        start, end = center - half, center + half
    else:
        now = time.time()
        start, end = now - span * 60, now
    rows = loki_query_range(os.environ["LOKI_URL"], logql, start, end, limit=100)
    return _bounded([[ts, line[:500]] for ts, line in rows])


def query_netbox(path: str, params: dict | None = None) -> dict:
    """GET a NetBox REST API path — the network source of truth.

    Examples: query_netbox("/api/dcim/devices/", {"name": "hub-e"}),
    query_netbox("/api/dcim/cables/", {"label": "FOC-RING-EI20E"}).
    Device tags carry the agencies a cabinet serves. Returns the JSON
    body ({"error": ...} on HTTP failure)."""
    # Be liberal: models naturally write REST paths with query strings
    # (?name=hub-e). Split it off and fold into params — the path part
    # still faces the allowlist, params still go through urlencode.
    if path and "?" in path:
        from urllib.parse import parse_qsl
        path, _, qs = path.partition("?")
        params = {**dict(parse_qsl(qs)), **(params or {})}
    if not _NETBOX_PATH_RE.fullmatch(path or ""):
        raise ModelRetry(
            "path must be a NetBox REST path like /api/dcim/devices/ "
            "(filters go in params)")
    _repeat_guard("query_netbox",
                  (path, tuple(sorted((params or {}).items()))))
    from netbox_client import Client
    try:
        return _bounded(Client().get(path, **(params or {})))
    except Exception as e:
        return {"error": str(e)}


def gnmi_get(node: str, path: str) -> dict:
    """gNMI Get against a live SR Linux node (names tmc-* or hub-*).

    SR Linux uses its NATIVE YANG model, not OpenConfig — there is NO
    `/state/` container. Interface state leaves live directly under the
    interface: admin-state, oper-state, oper-down-reason, last-change, mtu.
    So query the interface subtree and read those leaves directly:
    gnmi_get("hub-e", "/interface[name=ethernet-1/2]") returns admin-state
    and oper-state and oper-down-reason — the fields that distinguish an
    administrative shutdown (admin-state=disable) from a physical/hardware
    failure. Do NOT use OpenConfig-style paths like
    "/interface[name=ethernet-1/2]/state/admin-status" — they fail with
    "Path not valid - unknown element 'state'".

    Examples: gnmi_get("hub-e", "/interface[name=ethernet-1/1]"),
    gnmi_get("hub-e", "/network-instance[name=default]/protocols/"
    "srl_nokia-isis:isis/instance[name=atlas]") for IS-IS adjacencies.
    Returns decoded json_ietf notifications ({"error": ...} if the
    device is unreachable)."""
    if not _SRL_NODE_RE.fullmatch(node or ""):
        raise ModelRetry("node must be an SR Linux node: tmc-* or hub-* "
                         "(fc-* cabinets speak SNMP — use snmp_get)")
    _repeat_guard("gnmi_get", (node, path))
    try:
        out = gnmi_readonly.get(node, path)
    except ValueError as e:
        raise ModelRetry(str(e))
    except Exception as e:
        return {"error": str(e)}
    # pygnmi yields None for an empty notification; a None tool return
    # serializes to a null tool message, which some OpenAI-compatible
    # servers (Ollama) reject with 400 invalid message content.
    return _bounded(out) if out is not None else {"error": "empty gNMI response"}


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
    _repeat_guard("snmp_get", (node, oid))
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
