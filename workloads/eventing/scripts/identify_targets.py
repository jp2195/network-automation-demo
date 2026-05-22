"""Pull affected node/interface from the alert, then derive the peer end
of the cut link and the affected node's two ring neighbors. Extracted
from wft-incident-collector.yaml's identify-targets inline block.

Optimization vs. original: query backbone links of the affected node
once, then iterate to fill in peers + ring neighbors. Drops 3-4
sequential round-trips to roughly 1 + N (N = ring count, typically 2).
"""

import json
import os
import urllib.parse
import urllib.request


def query(prom_url, q):
    url = prom_url + "/api/v1/query?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())["data"]["result"]


def main():
    alert = json.loads(os.environ["ALERT_JSON"])
    a = alert["alerts"][0]
    labels = a["labels"]
    affected = labels.get("node", "")
    iface    = labels.get("interface", "")
    link_id  = labels.get("link_id", "")

    prom = os.environ["PROM_URL"].rstrip("/")

    # One query to find every backbone link touching the affected node.
    local_links_rows = query(
        prom,
        f'group by (link_id) (link_membership_info{{node="{affected}", link_kind="backbone"}})',
    )
    local_link_ids = sorted({r["metric"]["link_id"] for r in local_links_rows})

    # Peer end of the CUT link (the (node, interface) on link_id with
    # node != affected).
    peer_node, peer_iface = "", ""
    for row in query(prom, f'link_membership_info{{link_id="{link_id}", node!="{affected}"}}'):
        peer_node = row["metric"].get("node", "")
        peer_iface = row["metric"].get("interface", "")
        break

    # Ring neighbors: other backbone links touching `affected`, grab
    # the OTHER endpoint of each.
    ring_neighbors = []
    for lid in local_link_ids:
        if lid == link_id:
            continue
        rows = query(
            prom,
            f'group by (node) (link_membership_info{{link_id="{lid}", node!="{affected}"}})',
        )
        for row in rows:
            ring_neighbors.append(row["metric"]["node"])

    ring_a = ring_neighbors[0] if len(ring_neighbors) > 0 else ""
    ring_b = ring_neighbors[1] if len(ring_neighbors) > 1 else ""

    os.makedirs("/tmp/argo", exist_ok=True)
    for k, v in (
        ("affected", affected),
        ("interface", iface),
        ("link_id", link_id),
        ("peer_node", peer_node),
        ("peer_interface", peer_iface),
        ("ring_a", ring_a),
        ("ring_b", ring_b),
    ):
        with open(f"/tmp/argo/{k}", "w") as f:
            f.write(v)
    print(
        f"affected={affected}/{iface} peer={peer_node}/{peer_iface} "
        f"ring=[{ring_a},{ring_b}] link={link_id}",
        flush=True,
    )


if __name__ == "__main__":
    main()
