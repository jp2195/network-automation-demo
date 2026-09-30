"""Unit tests for analyze_impact backup-path derivation (G2).

Run: cd workloads/eventing/scripts && python3 -m unittest test_analyze_impact -v
"""

import os
import unittest

import analyze_impact
from constants import LINK_KIND_BACKBONE, LINK_KIND_CABINET, ROLE_FIELD_CABINET


class BackupPathTest(unittest.TestCase):
    def setUp(self):
        self._orig = analyze_impact.prom_query
        os.environ["PROM_URL"] = "http://prom"

    def tearDown(self):
        analyze_impact.prom_query = self._orig
        os.environ.pop("PROM_URL", None)

    def _stub_down(self, link_ids):
        analyze_impact.prom_query = lambda url, expr: [
            {"metric": {"link_id": lid}} for lid in link_ids
        ]

    def test_cabinet_uplink_has_no_modeled_backup(self):
        bp = analyze_impact.compute_backup_path(
            {"link_kind": LINK_KIND_CABINET, "link_id": "ring-nw-n"})
        self.assertFalse(bp["available"])
        self.assertEqual(bp["state"], "none")
        self.assertIn("single-homed", bp["detail"])

    def test_backbone_ring_intact_is_up(self):
        # Only the failed link itself is down -> corridor ring still a path.
        self._stub_down(["ring-n-e"])
        bp = analyze_impact.compute_backup_path(
            {"link_kind": LINK_KIND_BACKBONE, "link_id": "ring-n-e"})
        self.assertTrue(bp["available"])
        self.assertEqual(bp["state"], "up")
        self.assertEqual(bp["via"], "corridor ring")

    def test_second_concurrent_ring_failure_is_degraded(self):
        self._stub_down(["ring-n-e", "ring-e-i20e"])
        bp = analyze_impact.compute_backup_path(
            {"link_kind": LINK_KIND_BACKBONE, "link_id": "ring-n-e"})
        self.assertTrue(bp["available"])
        self.assertEqual(bp["state"], "degraded")
        self.assertIn("ring-e-i20e", bp["detail"])

    def test_cabinet_by_alertname_without_link_kind(self):
        # SNMP cabinet alert carries no link_kind/link_id labels.
        bp = analyze_impact.compute_backup_path(
            {"name": "CabinetInterfaceOperDown", "link_id": None})
        self.assertFalse(bp["available"])
        self.assertEqual(bp["state"], "none")
        self.assertIn("single-homed", bp["detail"])

    def test_cabinet_by_affected_role(self):
        bp = analyze_impact.compute_backup_path(
            {"link_id": None}, affected_role=ROLE_FIELD_CABINET)
        self.assertFalse(bp["available"])
        self.assertEqual(bp["state"], "none")

    def test_no_prom_url_does_not_crash(self):
        os.environ.pop("PROM_URL", None)
        bp = analyze_impact.compute_backup_path(
            {"link_kind": LINK_KIND_BACKBONE, "link_id": "ring-n-e"})
        self.assertEqual(bp["state"], "up")  # no evidence of another failure


def _cable(cid, a_dev, a_if, b_dev, b_if):
    def term(dev, iface):
        return {"object_type": "dcim.interface",
                "object": {"device": {"name": dev}, "name": iface}}
    return {"id": cid, "label": f"c{cid}",
            "a_terminations": [term(a_dev, a_if)],
            "b_terminations": [term(b_dev, b_if)]}


# hub-i20e: two ring links + the fc-i20e cabinet drop.
HUB_CABLES = [
    _cable(1, "hub-i20e", "ethernet-1/1", "hub-i20w", "ethernet-1/2"),
    _cable(2, "hub-i20e", "ethernet-1/2", "hub-e", "ethernet-1/1"),
    _cable(3, "hub-i20e", "ethernet-1/4", "fc-i20e", "eth1"),
]


class FailedCablePeersTest(unittest.TestCase):
    def _devices(self, peers):
        return [p["device"] for p in peers]

    def test_ring_cut_only_reports_the_ring_peer(self):
        # A ring cut must not drag the hub's cabinet in as downstream —
        # that would falsely escalate every hub fault to severity high.
        peers = analyze_impact.failed_cable_peers(
            HUB_CABLES, "hub-i20e", cable_id=2, interface="ethernet-1/2")
        self.assertEqual(self._devices(peers), ["hub-e"])

    def test_cabinet_drop_reports_the_cabinet(self):
        peers = analyze_impact.failed_cable_peers(
            HUB_CABLES, "hub-i20e", cable_id=3, interface="ethernet-1/4")
        self.assertEqual(self._devices(peers), ["fc-i20e"])

    def test_interface_match_when_enrichment_has_no_cable_id(self):
        peers = analyze_impact.failed_cable_peers(
            HUB_CABLES, "hub-i20e", cable_id=None, interface="ethernet-1/2")
        self.assertEqual(self._devices(peers), ["hub-e"])

    def test_device_wide_alert_falls_back_to_all_peers(self):
        # No failed interface (e.g. ConfigDrift) → device-wide scope.
        peers = analyze_impact.failed_cable_peers(
            HUB_CABLES, "hub-i20e", cable_id=None, interface="")
        self.assertEqual(sorted(self._devices(peers)),
                         ["fc-i20e", "hub-e", "hub-i20w"])

    def test_unknown_interface_falls_back_to_all_peers(self):
        # Degraded enrichment must not silently under-report impact.
        peers = analyze_impact.failed_cable_peers(
            HUB_CABLES, "hub-i20e", cable_id=None, interface="ethernet-1/9")
        self.assertEqual(len(peers), 3)



class StrandedCabinetTests(unittest.TestCase):
    def _devices(self, peers):
        return [p["device"] for p in peers]

    def test_one_ring_link_down_strands_nothing(self):
        self.assertEqual(analyze_impact.stranded_cabinet_peers(
            HUB_CABLES, "hub-i20e", down_ifaces={"ethernet-1/2"}), [])

    def test_both_ring_links_down_strands_the_cabinet(self):
        # hurricane: the hub is cut off from the ring on both sides, so its
        # single-homed cabinet is isolated even though its own drop is up.
        peers = analyze_impact.stranded_cabinet_peers(
            HUB_CABLES, "hub-i20e", down_ifaces={"ethernet-1/1", "ethernet-1/2"})
        self.assertEqual(self._devices(peers), ["fc-i20e"])

    def test_device_without_backbone_links_strands_nothing(self):
        cab_only = [c for c in HUB_CABLES if c["id"] == 3]
        self.assertEqual(analyze_impact.stranded_cabinet_peers(
            cab_only, "hub-i20e", down_ifaces={"ethernet-1/4"}), [])

if __name__ == "__main__":
    unittest.main()
