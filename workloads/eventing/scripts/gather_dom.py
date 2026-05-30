"""Pull DOM (transceiver) readings for both ends of the cut link and
emit a markdown table. Extracted from wft-incident-collector.yaml's
gather-dom inline block."""

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

    print(f"## DOM snapshot — link {link}\n")
    print("| End | Node | Interface | Temp (°C) | Rx (dBm) | Tx (dBm) | Bias (mA) |")
    print("|-----|------|-----------|-----------|----------|----------|-----------|")
    for end, n, i in [
        ("affected", os.environ["AFFECTED"],  os.environ["AFFECTED_IFACE"]),
        ("peer",     os.environ["PEER"],      os.environ["PEER_IFACE"]),
    ]:
        if not n or not i:
            continue
        t  = temp.get((n, i), math.nan)
        rx = rxp.get((n, i), math.nan)
        tx = txp.get((n, i), math.nan)
        ba = bias.get((n, i), math.nan)
        print(f"| {end} | {n} | {i} | {t:.2f} | {rx:.2f} | {tx:.2f} | {ba:.2f} |")


if __name__ == "__main__":
    main()
