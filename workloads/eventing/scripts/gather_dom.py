"""Pull DOM (transceiver) readings for both ends of the cut link and emit an
aligned monospace table. Extracted from wft-incident-collector.yaml's
gather-dom inline block. Aligned columns (not a markdown pipe table) so it
renders cleanly both in a Slack code fence and in the .md bundle."""

import math
import os

from prom import prom_query


def _latest(prom, metric, link):
    rows = prom_query(prom, f'{metric}{{link_id="{link}"}}')
    return {
        (r["metric"]["node"], r["metric"]["interface"]): float(r["value"][1])
        for r in rows
    }


def main():
    prom = os.environ["PROM_URL"].rstrip("/")
    link = os.environ["LINK_ID"]

    temp = _latest(prom, "dom_temperature_celsius", link)
    rxp  = _latest(prom, "dom_rx_power_dbm",        link)
    txp  = _latest(prom, "dom_tx_power_dbm",        link)
    bias = _latest(prom, "dom_bias_current_milliamps", link)

    headers = ["End", "Node", "Interface", "Temp °C", "Rx dBm", "Tx dBm", "Bias mA"]
    rows = []
    for end, n, i in [
        ("affected", os.environ["AFFECTED"],  os.environ["AFFECTED_IFACE"]),
        ("peer",     os.environ["PEER"],      os.environ["PEER_IFACE"]),
    ]:
        if not n or not i:
            continue

        def val(m):
            v = m.get((n, i), math.nan)
            return "—" if math.isnan(v) else f"{v:.2f}"

        rows.append([end, n, i, val(temp), val(rxp), val(txp), val(bias)])

    table = [headers] + rows
    widths = [max(len(r[c]) for r in table) for c in range(len(headers))]

    def fmt(cells):
        # text columns (0–2) left-aligned, numeric columns right-aligned.
        return "  ".join(
            cells[c].rjust(widths[c]) if c >= 3 else cells[c].ljust(widths[c])
            for c in range(len(cells)))

    print(f"DOM snapshot — link {link}\n")
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


if __name__ == "__main__":
    main()
