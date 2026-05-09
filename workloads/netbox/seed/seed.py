#!/usr/bin/env python3
"""Seed NetBox from spec-derived seed.json.

Idempotent. Uses lookup-by-slug-or-name and only creates missing items.
Resolves human-friendly FK references (slug/name) to NetBox IDs at apply time.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse


URL = os.environ["NETBOX_URL"].rstrip("/")
USERNAME = os.environ["NETBOX_USERNAME"]
PASSWORD = os.environ["NETBOX_PASSWORD"]
SEED = os.environ.get("SEED_FILE", "/seed/seed.json")

# Populated by provision_token() once the API is reachable. NetBox 4.x
# hashes API tokens at rest, so we can't pin a specific value via env;
# instead we mint a fresh one with username+password each run.
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def http(method, path, body=None, params=None):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{URL}{path}{qs}"
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(8):
        req = urllib.request.Request(url, method=method, headers=HEADERS, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            # Backs off when the netbox pod is under load or restarting.
            time.sleep(2 + attempt * 2)
    raise last_err


def wait_ready():
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            code, _ = http("GET", "/api/status/")
            if code == 200:
                print("netbox API ready", flush=True)
                return
        except Exception as e:
            print(f"waiting for netbox: {e}", flush=True)
        time.sleep(5)
    sys.exit("timed out waiting for /api/status/")


def provision_token():
    """Mint a fresh API token via /api/users/tokens/provision/.

    Force v1 (legacy `Token <plaintext>` header). NetBox 4.6 defaults new
    tokens to v2 (peppered HMAC with `Bearer nbt_<key>.<plaintext>`), but
    v2 requires API_TOKEN_PEPPERS in NetBox's settings — netbox-chart 8.2.9
    doesn't expose that config, so v2 tokens authenticate as 403.
    """
    code, body = http(
        "POST",
        "/api/users/tokens/provision/",
        body={"username": USERNAME, "password": PASSWORD, "version": 1},
    )
    if code not in (200, 201) or not body.get("token"):
        sys.exit(f"failed to provision API token ({code}): {body}")
    HEADERS["Authorization"] = f"Token {body['token']}"
    print(f"provisioned v{body.get('version')} API token id={body.get('id')}", flush=True)


def find_id(endpoint, **filters):
    code, body = http("GET", endpoint, params=filters)
    if code == 200 and body.get("results"):
        return body["results"][0]["id"]
    return None


def upsert(endpoint, lookup, payload, label):
    existing = find_id(endpoint, **lookup)
    if existing is not None:
        print(f"  exists: {label} (id={existing})", flush=True)
        return existing
    code, body = http("POST", endpoint, body=payload)
    if code in (200, 201):
        print(f"  created: {label} (id={body['id']})", flush=True)
        return body["id"]
    print(f"  FAILED ({code}) {label}: {body}", flush=True)
    sys.exit(1)


def patch(endpoint, item_id, payload, label):
    code, body = http("PATCH", f"{endpoint}{item_id}/", body=payload)
    if code in (200, 201):
        print(f"  patched: {label}", flush=True)
        return
    print(f"  FAILED patch ({code}) {label}: {body}", flush=True)
    sys.exit(1)


def main():
    with open(SEED) as f:
        data = json.load(f)

    wait_ready()
    provision_token()

    print("== custom fields ==", flush=True)
    for cf in data.get("custom_fields", []):
        upsert(
            "/api/extras/custom-fields/",
            {"name": cf["name"]},
            cf,
            f"custom-field/{cf['name']}",
        )

    print("== regions ==", flush=True)
    region_ids = {}
    for r in data.get("regions", []):
        region_ids[r["slug"]] = upsert(
            "/api/dcim/regions/",
            {"slug": r["slug"]},
            {"slug": r["slug"], "name": r["name"],
             "description": r.get("description", "")},
            f"region/{r['slug']}",
        )

    print("== site_groups ==", flush=True)
    sg_ids = {}
    for g in data.get("site_groups", []):
        sg_ids[g["slug"]] = upsert(
            "/api/dcim/site-groups/",
            {"slug": g["slug"]},
            {"slug": g["slug"], "name": g["name"],
             "description": g.get("description", "")},
            f"site-group/{g['slug']}",
        )

    print("== rirs ==", flush=True)
    rir_ids = {}
    for r in data.get("rirs", []):
        rir_ids[r["slug"]] = upsert(
            "/api/ipam/rirs/",
            {"slug": r["slug"]},
            {"slug": r["slug"], "name": r["name"],
             "is_private": r.get("is_private", False)},
            f"rir/{r['slug']}",
        )

    print("== sites ==", flush=True)
    site_ids = {}
    for s in data.get("sites", []):
        payload = {"slug": s["slug"], "name": s["name"], "status": "active",
                   "latitude": s.get("latitude"), "longitude": s.get("longitude")}
        if s.get("region"):
            payload["region"] = region_ids[s["region"]]
        if s.get("group"):
            payload["group"] = sg_ids[s["group"]]
        site_ids[s["slug"]] = upsert(
            "/api/dcim/sites/",
            {"slug": s["slug"]},
            payload,
            f"site/{s['slug']}",
        )

    print("== asns ==", flush=True)
    for a in data.get("asns", []):
        existing = find_id("/api/ipam/asns/", asn=a["asn"])
        payload = {
            "asn": a["asn"],
            "rir": rir_ids[a["rir"]],
            "description": a.get("description", ""),
        }
        if a.get("sites"):
            payload["sites"] = [site_ids[slug] for slug in a["sites"]]
        if existing is not None:
            print(f"  exists: asn/{a['asn']} (id={existing})", flush=True)
            patch("/api/ipam/asns/", existing, payload, f"asn/{a['asn']}")
        else:
            code, body = http("POST", "/api/ipam/asns/", body=payload)
            if code in (200, 201):
                print(f"  created: asn/{a['asn']} (id={body['id']})", flush=True)
            else:
                sys.exit(f"FAILED asn/{a['asn']}: {body}")

    print("== tenants ==", flush=True)
    tenant_ids = {}
    for t in data.get("tenants", []):
        tenant_ids[t["slug"]] = upsert(
            "/api/tenancy/tenants/",
            {"slug": t["slug"]},
            {"slug": t["slug"], "name": t["name"]},
            f"tenant/{t['slug']}",
        )

    print("== owner_groups ==", flush=True)
    og_ids = {}
    for og in data.get("owner_groups", []):
        # OwnerGroup has no `slug`; look up by name.
        og_ids[og["name"]] = upsert(
            "/api/users/owner-groups/",
            {"name": og["name"]},
            {"name": og["name"], "description": og.get("description", "")},
            f"owner-group/{og['name']}",
        )

    print("== owners ==", flush=True)
    owner_ids = {}
    for o in data.get("owners", []):
        payload = {"name": o["name"], "description": o.get("description", "")}
        if o.get("group"):
            payload["group"] = og_ids[o["group"]]
        owner_ids[o["name"]] = upsert(
            "/api/users/owners/",
            {"name": o["name"]},
            payload,
            f"owner/{o['name']}",
        )

    print("== manufacturers ==", flush=True)
    mfr_ids = {}
    for m in data.get("manufacturers", []):
        mfr_ids[m["slug"]] = upsert(
            "/api/dcim/manufacturers/",
            {"slug": m["slug"]},
            {"slug": m["slug"], "name": m["name"]},
            f"manufacturer/{m['slug']}",
        )

    print("== device_types ==", flush=True)
    dt_ids = {}
    for dt in data.get("device_types", []):
        dt_ids[dt["slug"]] = upsert(
            "/api/dcim/device-types/",
            {"slug": dt["slug"]},
            {"slug": dt["slug"], "model": dt["model"],
             "manufacturer": mfr_ids[dt["manufacturer"]]},
            f"device_type/{dt['slug']}",
        )

    print("== device_roles ==", flush=True)
    role_ids = {}
    for r in data.get("device_roles", []):
        role_ids[r["slug"]] = upsert(
            "/api/dcim/device-roles/",
            {"slug": r["slug"]},
            {"slug": r["slug"], "name": r["name"], "color": r.get("color", "9e9e9e")},
            f"device_role/{r['slug']}",
        )

    print("== devices ==", flush=True)
    device_ids = {}
    for d in data.get("devices", []):
        payload = {
            "name": d["name"],
            "site": site_ids[d["site"]],
            "role": role_ids[d["role"]],
            "device_type": dt_ids[d["device_type"]],
            "status": d.get("status", "active"),
            "custom_fields": d.get("custom_fields", {}),
        }
        device_ids[d["name"]] = upsert(
            "/api/dcim/devices/",
            {"name": d["name"]},
            payload,
            f"device/{d['name']}",
        )

    print("== interfaces ==", flush=True)
    iface_ids = {}
    for i in data.get("interfaces", []):
        key = (i["device"], i["name"])
        iface_ids[key] = upsert(
            "/api/dcim/interfaces/",
            {"device_id": device_ids[i["device"]], "name": i["name"]},
            {"device": device_ids[i["device"]], "name": i["name"],
             "type": i.get("type", "1000base-t"),
             "description": i.get("description", "")},
            f"iface/{i['device']}/{i['name']}",
        )

    print("== ip_addresses ==", flush=True)
    ip_ids = {}
    for ip in data.get("ip_addresses", []):
        key = (ip["device"], ip["interface"])
        iface_id = iface_ids[key]
        existing = find_id("/api/ipam/ip-addresses/", address=ip["address"])
        if existing is not None:
            ip_ids[ip["address"]] = existing
            print(f"  exists: ip/{ip['address']} (id={existing})", flush=True)
            continue
        payload = {
            "address": ip["address"],
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": iface_id,
            "status": "active",
        }
        code, body = http("POST", "/api/ipam/ip-addresses/", body=payload)
        if code in (200, 201):
            ip_ids[ip["address"]] = body["id"]
            print(f"  created: ip/{ip['address']} (id={body['id']})", flush=True)
        else:
            sys.exit(f"FAILED ip/{ip['address']}: {body}")

    print("== device.primary_ip4 ==", flush=True)
    for d in data.get("devices", []):
        if not d.get("primary_ip4"):
            continue
        ip_id = ip_ids.get(d["primary_ip4"])
        if ip_id is None:
            print(f"  skip (ip not found): {d['name']} -> {d['primary_ip4']}", flush=True)
            continue
        patch("/api/dcim/devices/", device_ids[d["name"]],
              {"primary_ip4": ip_id}, f"device/{d['name']} primary_ip4")

    print("== cables ==", flush=True)
    cable_ids = {}
    for c in data.get("cables", []):
        a_iface = iface_ids[(c["a"]["device"], c["a"]["interface"])]
        b_iface = iface_ids[(c["b"]["device"], c["b"]["interface"])]
        existing = find_id("/api/dcim/cables/", label=c["label"])
        if existing is not None:
            cable_ids[c["label"]] = existing
            print(f"  exists: cable/{c['label']} (id={existing})", flush=True)
            continue
        payload = {
            "label": c["label"],
            "status": c.get("status", "connected"),
            "a_terminations": [{"object_type": "dcim.interface", "object_id": a_iface}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": b_iface}],
            "description": c.get("description", ""),
            "custom_fields": c.get("custom_fields", {}),
        }
        if c.get("type"):
            payload["type"] = c["type"]
        if c.get("owner") and c["owner"] in owner_ids:
            payload["owner"] = owner_ids[c["owner"]]
        if c.get("length"):
            payload["length"] = c["length"]
            payload["length_unit"] = c.get("length_unit", "km")
        if c.get("install_date"):
            payload["install_date"] = c["install_date"]
        code, body = http("POST", "/api/dcim/cables/", body=payload)
        if code in (200, 201):
            cable_ids[c["label"]] = body["id"]
            print(f"  created: cable/{c['label']} (id={body['id']})", flush=True)
        else:
            sys.exit(f"FAILED cable/{c['label']}: {body}")

    print("== journal_entries ==", flush=True)
    for j in data.get("journal_entries", []):
        otype = j["assigned_object_type"]
        if otype == "dcim.cable":
            obj_id = cable_ids.get(j["assigned_object"]["label"])
        elif otype == "dcim.device":
            obj_id = device_ids.get(j["assigned_object"]["name"])
        else:
            print(f"  skip (unsupported assigned_object_type): {otype}", flush=True)
            continue
        if obj_id is None:
            print(f"  skip (object not found): {j['assigned_object']}", flush=True)
            continue
        payload = {
            "assigned_object_type": otype,
            "assigned_object_id": obj_id,
            "kind": j.get("kind", "info"),
            "comments": j.get("comments", ""),
        }
        code, body = http("POST", "/api/extras/journal-entries/", body=payload)
        if code in (200, 201):
            print(f"  created: journal/{otype}/{obj_id}", flush=True)
        else:
            print(f"  FAILED journal ({code}): {body}", flush=True)

    print("seed complete", flush=True)


if __name__ == "__main__":
    main()
