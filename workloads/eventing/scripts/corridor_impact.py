"""Corridor what-if: which devices go dark if a corridor's fiber is cut.

The chat lane's deterministic blast-radius tool, computed from NetBox —
the network source of truth — never guessed by the model. Two GETs fetch
every cable (terminations inline, corridor custom field) and every device
(role, site, agency tags); then it's pure graph work: cut the cables whose
corridor matches, build adjacency from the survivors' terminations, BFS
from the TMCs, and whatever routers can no longer be reached are the
impact. Agencies come off the isolated routers' tags; the ITS roster
(CCTV/signals/DMS/ramp meters — seeded as devices at each cabinet's site)
is counted at the isolated sites.
"""

from netbox_client import Client

_ROUTER_ROLES = ("tmc", "corridor-hub", "field-cabinet")
_ITS_ROLES = ("cctv-camera", "signal-controller", "dms", "ramp-meter")


def _norm(name):
    return "".join(c for c in (name or "").lower() if c.isalnum())


def match_corridors(query, corridors):
    """Corridors whose normalized name equals or extends the query —
    "I285" matches both "I-285" (the ring) and "I-285 East" (a spur
    along the same highway); "GA-400" matches only itself."""
    q = _norm(query)
    if not q:
        return set()
    return {c for c in corridors if _norm(c).startswith(q)}


def _cable_devices(cable):
    """Device names on each side of a cable, from the inline terminations."""
    sides = []
    for side in ("a_terminations", "b_terminations"):
        names = {((t.get("object") or {}).get("device") or {}).get("name")
                 for t in cable.get(side, [])
                 if t.get("object_type") == "dcim.interface"}
        sides.append({n for n in names if n})
    return sides


def _reachable(cables, cut_labels, sources):
    adj = {}
    for c in cables:
        if c.get("label") in cut_labels:
            continue
        side_a, side_b = _cable_devices(c)
        for a in side_a:
            for b in side_b:
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)
    seen, frontier = set(sources), list(sources)
    while frontier:
        for peer in adj.get(frontier.pop(), ()):
            if peer not in seen:
                seen.add(peer)
                frontier.append(peer)
    return seen


def compute(corridor, cables, devices):
    """Pure what-if over pre-fetched NetBox cable + device records."""
    corridor_of = {c.get("label"): (c.get("custom_fields") or {}).get("corridor", "")
                   for c in cables}
    all_corridors = {c for c in corridor_of.values() if c}
    matched = match_corridors(corridor, all_corridors)
    if not matched:
        return {"error": f"no corridor matches {corridor!r}",
                "available_corridors": sorted(all_corridors)}

    cut_labels = {label for label, c in corridor_of.items() if c in matched}
    routers = {d["name"]: d for d in devices
               if (d.get("role") or {}).get("slug") in _ROUTER_ROLES}
    sources = {n for n, d in routers.items()
               if (d.get("role") or {}).get("slug") == "tmc"}
    reachable = _reachable(cables, cut_labels, sources)

    isolated = [{"device": n,
                 "role": (d.get("role") or {}).get("slug", ""),
                 "site": (d.get("site") or {}).get("slug", "")}
                for n, d in sorted(routers.items()) if n not in reachable]

    if any(d["role"] == "field-cabinet" for d in isolated):
        severity = "high"
    elif isolated:
        severity = "medium"
    else:
        severity = "low"

    agencies = sorted({tag["slug"]
                       for d in isolated
                       for tag in routers[d["device"]].get("tags", [])
                       if tag.get("slug")})
    dark_sites = {d["site"] for d in isolated if d["site"]}
    assets = {}
    for d in devices:
        role = (d.get("role") or {}).get("slug", "")
        if role in _ITS_ROLES and (d.get("site") or {}).get("slug") in dark_sites:
            assets[role] = assets.get(role, 0) + 1

    return {
        "corridor_query": corridor,
        "matched_corridors": sorted(matched),
        "links_cut": [{"cable_label": label, "corridor": corridor_of[label]}
                      for label in sorted(cut_labels)],
        "isolated_devices": isolated,
        "affected_agencies": agencies,
        "its_assets_lost": assets,
        "severity_class": severity,
    }


def _fetch():
    nb = Client()
    cables = nb.get("/api/dcim/cables/", limit=200).get("results", [])
    devices = nb.get("/api/dcim/devices/", limit=500).get("results", [])
    return cables, devices


def corridor_impact(corridor: str) -> dict:
    """What-if: if this corridor's fiber is cut, which devices lose all
    connectivity to the TMCs? Deterministic graph reachability over the
    NetBox source of truth (cables + devices) — NOT a guess. Returns the
    matched corridors, every cable cut, the isolated devices, the agencies
    riding them, and the ITS assets (CCTV, signals, DMS, ramp meters)
    that go dark. Corridor names are forgiving: "I285", "i-285", "GA400"
    all work. Unknown names return the list of valid corridors."""
    try:
        cables, devices = _fetch()
    except Exception as e:
        return {"error": f"NetBox lookup failed: {e}"}
    return compute(corridor, cables, devices)
