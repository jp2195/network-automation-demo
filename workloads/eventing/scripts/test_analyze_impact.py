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


if __name__ == "__main__":
    unittest.main()
