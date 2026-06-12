"""Unit tests for drift_compare.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with fakeredis --with valkey python3 -m unittest test_drift -v
"""

import json
import unittest

import fakeredis

from constants import REMEDIATION_ACTIVE_PREFIX
from drift_compare import (
    build_alerts,
    diff_node,
    env_name,
    parse_live,
    suppress_remediated,
)

# Shape of `gnmic get --type config -e json_ietf` output: a list of
# notifications, each with updates[].values keyed by path, holding
# json_ietf payloads. parse_live() walks it structurally so module-prefix
# differences don't matter.
GNMIC_OUTPUT = json.dumps([{
    "source": "atlanta-hub-e.clabernetes.svc.cluster.local:57400",
    "timestamp": 1,
    "updates": [
        {
            "Path": "interface",
            "values": {
                "srl_nokia-interfaces:interface": [
                    {"name": "ethernet-1/1", "admin-state": "enable"},
                    {"name": "ethernet-1/2", "admin-state": "disable"},
                    {"name": "lo0", "admin-state": "enable"},
                ],
            },
        },
        {
            "Path": "network-instance[name=default]/protocols/isis/instance[name=atlas]",
            "values": {
                "srl_nokia-network-instance:network-instance/protocols/srl_nokia-isis:isis/instance": {
                    "name": "atlas",
                    "interface": [
                        {"interface-name": "ethernet-1/1.0",
                         "level": [{"level-number": 2, "metric": 16777214}]},
                        {"interface-name": "ethernet-1/2.0",
                         "level": [{"level-number": 2}]},
                        {"interface-name": "lo0.0"},
                    ],
                },
            },
        },
    ],
}])

EXPECTED_NODE = {
    "interfaces": {
        "ethernet-1/1": {"link_id": "ring-e-i20e"},
        "ethernet-1/2": {"link_id": "ring-n-e"},
    },
    "isis_interfaces": ["ethernet-1/1.0", "ethernet-1/2.0", "lo0.0"],
}


class ParseLiveTests(unittest.TestCase):
    def test_extracts_admin_state_and_metric_presence(self):
        live = parse_live(GNMIC_OUTPUT)
        self.assertEqual(live["interfaces"]["ethernet-1/1"], "enable")
        self.assertEqual(live["interfaces"]["ethernet-1/2"], "disable")
        self.assertTrue(live["isis"]["ethernet-1/1.0"])    # metric present
        self.assertFalse(live["isis"]["ethernet-1/2.0"])   # level but no metric
        self.assertFalse(live["isis"]["lo0.0"])            # no level at all

    def test_unreachable_returns_none(self):
        self.assertIsNone(parse_live("[]"))
        self.assertIsNone(parse_live(""))
        self.assertIsNone(parse_live("not json"))


class DiffNodeTests(unittest.TestCase):
    def setUp(self):
        self.live = parse_live(GNMIC_OUTPUT)

    def test_detects_admin_state_and_metric_drift(self):
        drifts = diff_node("hub-e", EXPECTED_NODE, self.live)
        kinds = {(d["kind"], d["interface"]) for d in drifts}
        self.assertIn(("admin-state", "ethernet-1/2"), kinds)
        self.assertIn(("isis-metric", "ethernet-1/1.0"), kinds)
        self.assertEqual(len(drifts), 2)

    def test_metric_drift_carries_link_of_base_interface(self):
        drifts = diff_node("hub-e", EXPECTED_NODE, self.live)
        metric = [d for d in drifts if d["kind"] == "isis-metric"][0]
        self.assertEqual(metric["link_id"], "ring-e-i20e")

    def test_missing_interface_is_drift(self):
        exp = {"interfaces": {"ethernet-1/9": {"link_id": "ghost"}},
               "isis_interfaces": []}
        drifts = diff_node("hub-e", exp, self.live)
        self.assertEqual(drifts[0]["kind"], "missing-interface")

    def test_clean_node_yields_no_drift(self):
        exp = {"interfaces": {"ethernet-1/1": {"link_id": "ring-e-i20e"}},
               "isis_interfaces": ["ethernet-1/2.0", "lo0.0"]}
        self.assertEqual(diff_node("hub-e", exp, self.live), [])


class SuppressionTests(unittest.TestCase):
    def setUp(self):
        self.vk = fakeredis.FakeRedis(decode_responses=True)
        self.drifts = diff_node("hub-e", EXPECTED_NODE, parse_live(GNMIC_OUTPUT))

    def test_metric_drift_suppressed_when_remediation_active(self):
        self.vk.set(REMEDIATION_ACTIVE_PREFIX + "ring-e-i20e", "{}")
        kept, suppressed = suppress_remediated(self.drifts, self.vk)
        self.assertEqual([d["kind"] for d in suppressed], ["isis-metric"])
        self.assertEqual([d["kind"] for d in kept], ["admin-state"])

    def test_admin_state_drift_never_suppressed(self):
        self.vk.set(REMEDIATION_ACTIVE_PREFIX + "ring-n-e", "{}")
        kept, suppressed = suppress_remediated(self.drifts, self.vk)
        self.assertEqual(suppressed, [])
        self.assertEqual(len(kept), 2)

    def test_no_valkey_keeps_everything(self):
        kept, suppressed = suppress_remediated(self.drifts, None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(suppressed, [])


class BuildAlertsTests(unittest.TestCase):
    def test_alert_payload_shape(self):
        drifts = [{"node": "hub-e", "interface": "ethernet-1/2",
                   "link_id": "ring-n-e", "kind": "admin-state",
                   "detail": "ethernet-1/2 admin-state disable (rendered config says enable)"}]
        alerts = build_alerts(drifts)
        self.assertEqual(len(alerts), 1)
        labels = alerts[0]["labels"]
        self.assertEqual(labels["alertname"], "ConfigDrift")
        self.assertEqual(labels["severity"], "warning")
        self.assertEqual(labels["namespace"], "monitoring")  # AMC sub-route matcher
        self.assertEqual(labels["node"], "hub-e")
        self.assertEqual(labels["interface"], "ethernet-1/2")
        self.assertIn("endsAt", alerts[0])
        self.assertIn("SSOT", alerts[0]["annotations"]["description"])


class EnvNameTests(unittest.TestCase):
    def test_node_to_env(self):
        self.assertEqual(env_name("hub-i20e"), "LIVE_HUB_I20E")
        self.assertEqual(env_name("tmc-1"), "LIVE_TMC_1")


if __name__ == "__main__":
    unittest.main()
