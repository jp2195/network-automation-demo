"""Unit tests for remediate_decide.decide() and remediate_record helpers.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with fakeredis --with valkey python3 -m unittest test_remediate -v
"""

import unittest

import fakeredis

from constants import (
    REMEDIATION_ACTIVE_PREFIX,
    REMEDIATION_APPROVE_PREFIX,
    REMEDIATION_MODE_GATED,
    REMEDIATION_MODE_KEY,
)
from remediate_decide import decide

PROM = "http://prom.example"
LINK = "ring-e-i20e"

MEMBERS = [
    {"metric": {"node": "hub-e", "interface": "ethernet-1/1", "link_id": LINK}},
    {"metric": {"node": "hub-i20e", "interface": "ethernet-1/2", "link_id": LINK}},
]


def alert(status="firing", link_id=LINK, link_kind="backbone"):
    return {"alerts": [{
        "status": status,
        "fingerprint": "abc123",
        "labels": {
            "alertname": "SRLInterfaceErrorsHigh", "severity": "warning",
            "link_id": link_id, "link_kind": link_kind,
            "node": "hub-e", "interface": "ethernet-1/1",
        },
    }]}


def prom_stub(rows_by_substr):
    """Return a prom_query-compatible stub keyed on substring of the expr."""
    def fn(url, expr):
        for substr, rows in rows_by_substr.items():
            if substr in expr:
                return rows
        return []
    return fn


class DecideFiringTests(unittest.TestCase):
    def setUp(self):
        self.vk = fakeredis.FakeRedis(decode_responses=True)
        self.prom = prom_stub({"link_membership_info": MEMBERS})

    def test_auto_mode_costs_out_both_ends(self):
        out = decide(alert(), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "cost-out")
        self.assertEqual(out["node_a"], "hub-e")
        self.assertEqual(out["iface_a"], "ethernet-1/1.0")   # IS-IS subinterface name
        self.assertEqual(out["node_b"], "hub-i20e")
        self.assertEqual(out["iface_b"], "ethernet-1/2.0")
        self.assertTrue(self.vk.exists(REMEDIATION_ACTIVE_PREFIX + LINK))

    def test_second_firing_alert_is_idempotent(self):
        decide(alert(), self.vk, self.prom, PROM)
        out = decide(alert(), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "skip")

    def test_cabinet_link_is_skipped(self):
        out = decide(alert(link_kind="cabinet"), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "skip")

    def test_missing_link_id_is_skipped(self):
        out = decide(alert(link_id=""), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "skip")

    def test_unknown_link_members_is_skipped(self):
        out = decide(alert(), self.vk, prom_stub({}), PROM)
        self.assertEqual(out["action"], "skip")
        self.assertFalse(self.vk.exists(REMEDIATION_ACTIVE_PREFIX + LINK))


class DecideGatedTests(unittest.TestCase):
    def setUp(self):
        self.vk = fakeredis.FakeRedis(decode_responses=True)
        self.vk.set(REMEDIATION_MODE_KEY, REMEDIATION_MODE_GATED)
        self.prom = prom_stub({"link_membership_info": MEMBERS})

    def test_pre_approved_costs_out_and_consumes_token(self):
        self.vk.set(REMEDIATION_APPROVE_PREFIX + LINK, "1")
        out = decide(alert(), self.vk, self.prom, PROM, gate_timeout=0)
        self.assertEqual(out["action"], "cost-out")
        self.assertFalse(self.vk.exists(REMEDIATION_APPROVE_PREFIX + LINK))

    def test_gate_timeout_skips_without_claim(self):
        out = decide(alert(), self.vk, self.prom, PROM,
                     gate_timeout=0, sleep_fn=lambda s: None)
        self.assertEqual(out["action"], "skip")
        self.assertFalse(self.vk.exists(REMEDIATION_ACTIVE_PREFIX + LINK))

    def test_approval_arriving_during_poll(self):
        sleeps = []

        def sleep_fn(s):
            sleeps.append(s)
            self.vk.set(REMEDIATION_APPROVE_PREFIX + LINK, "1")

        out = decide(alert(), self.vk, self.prom, PROM,
                     gate_timeout=60, poll_interval=5, sleep_fn=sleep_fn)
        self.assertEqual(out["action"], "cost-out")
        self.assertEqual(len(sleeps), 1)


class DecideResolvedTests(unittest.TestCase):
    def setUp(self):
        self.vk = fakeredis.FakeRedis(decode_responses=True)
        self.prom = prom_stub({"link_membership_info": MEMBERS})

    def test_restore_when_remediation_active(self):
        self.vk.set(REMEDIATION_ACTIVE_PREFIX + LINK, "{}")
        out = decide(alert(status="resolved"), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "restore")
        # record (not decide) clears the claim, after the Sets succeed
        self.assertTrue(self.vk.exists(REMEDIATION_ACTIVE_PREFIX + LINK))

    def test_resolved_without_active_claim_skips(self):
        out = decide(alert(status="resolved"), self.vk, self.prom, PROM)
        self.assertEqual(out["action"], "skip")

    def test_resolved_while_sibling_warning_still_firing_skips(self):
        self.vk.set(REMEDIATION_ACTIVE_PREFIX + LINK, "{}")
        prom = prom_stub({
            "link_membership_info": MEMBERS,
            "ALERTS": [{"metric": {"alertname": "SRLOpticalDegrading"}}],
        })
        out = decide(alert(status="resolved"), self.vk, prom, PROM)
        self.assertEqual(out["action"], "skip")


if __name__ == "__main__":
    unittest.main()
