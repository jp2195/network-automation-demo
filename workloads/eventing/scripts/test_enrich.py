"""Unit tests for enrich.py's degrade contract.

enrich is the FIRST step of the incident pipeline, so anything it raises
takes the whole notification down with it — the alert is never announced.
These cover the failure modes that used to do exactly that.

Run: cd workloads/eventing/scripts && python3 -m unittest test_enrich -v
"""

import json
import io
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

os.environ.setdefault("NETBOX_URL", "http://netbox.invalid")
os.environ.setdefault("NETBOX_TOKEN", "test-token")

import enrich


ALERT = json.dumps({
    "alerts": [{
        "labels": {
            "alertname": "SRLInterfaceOperDown",
            "severity": "critical",
            "node": "hub-e",
            "interface": "ethernet-1/2",
            "link_id": "ring-n-e",
            "corridor": "I-285",
        },
        "startsAt": "2026-08-24T16:44:03.37Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "status": "firing",
        "fingerprint": "398c85567411b882",
    }]
})

DEVICE = {"results": [{
    "id": 3, "name": "hub-e",
    "role": {"slug": "corridor-hub"},
    "site": {"id": 7},
    "primary_ip4": {"address": "10.0.0.12/32"},
    "custom_fields": {"isis_sid": 16102},
}]}


def run_enrich():
    """Run enrich.main(), returning the parsed stdout enrichment."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        with mock.patch.dict(os.environ, {"ALERT_JSON": ALERT}):
            enrich.main()
    return json.loads(out.getvalue())


class DegradeContractTests(unittest.TestCase):
    def setUp(self):
        enrich._failures.clear()

    def tearDown(self):
        enrich._failures.clear()

    def test_netbox_outage_still_emits_usable_enrichment(self):
        """A NetBox 500/timeout used to raise straight out of enrich and
        fail the Workflow — no Slack card at all. It must degrade instead."""
        with mock.patch.object(enrich, "get",
                               side_effect=OSError("connection refused")):
            enrichment = run_enrich()

        # The alert block is derived from labels alone, so it survives intact
        # — that is what makes the degraded card still worth posting.
        self.assertEqual(enrichment["alert"]["node"], "hub-e")
        self.assertEqual(enrichment["alert"]["link_id"], "ring-n-e")
        self.assertEqual(enrichment["alert"]["fingerprint"], "398c85567411b882")
        self.assertEqual(enrichment["device"], {"name": "hub-e"})
        self.assertTrue(enrichment["degraded"])
        self.assertIn("netbox", enrichment["degraded"][0])

    def test_outage_is_not_reported_as_device_not_found(self):
        """A NetBox outage and a genuinely missing device are different
        operational facts and must not collapse into the same message."""
        with mock.patch.object(enrich, "get",
                               side_effect=OSError("connection refused")):
            enrichment = run_enrich()
        joined = " ".join(enrichment["degraded"])
        self.assertNotIn("not found in NetBox", joined)

    def test_missing_device_still_reports_not_found(self):
        with mock.patch.object(enrich, "get", return_value={"results": []}):
            enrichment = run_enrich()
        self.assertIn("not found in NetBox", " ".join(enrichment["degraded"]))

    def test_partial_outage_keeps_the_device_it_did_fetch(self):
        """Device lookup succeeds, the cable/site calls fail: keep what we
        have rather than throwing the whole enrichment away."""
        def flaky(path, **params):
            if path == "/api/dcim/devices/":
                return DEVICE
            raise OSError("upstream timeout")

        with mock.patch.object(enrich, "get", side_effect=flaky):
            enrichment = run_enrich()

        self.assertEqual(enrichment["device"]["name"], "hub-e")
        self.assertEqual(enrichment["device"]["role"], "corridor-hub")
        self.assertEqual(enrichment["cable"], {})
        self.assertTrue(enrichment["degraded"])

    def test_healthy_lookup_reports_no_degradation(self):
        def ok(path, **params):
            if path == "/api/dcim/devices/":
                return DEVICE
            if path == "/api/dcim/interfaces/":
                return {"results": [{"name": "ethernet-1/2",
                                     "type": {"value": "10gbase-x-sfpp"},
                                     "description": "to hub-n"}]}
            if path.startswith("/api/dcim/sites/"):
                return {"name": "Decatur", "slug": "decatur",
                        "latitude": 33.7748, "longitude": -84.2963}
            return {}

        with mock.patch.object(enrich, "get", side_effect=ok):
            enrichment = run_enrich()

        self.assertEqual(enrichment["degraded"], [])
        self.assertEqual(enrichment["device"]["site"], "Decatur")
        self.assertEqual(enrichment["interface"]["name"], "ethernet-1/2")


class DeviceNameDerivationTests(unittest.TestCase):
    """The fallback used only when an alert carries no `node` label: strip
    the clabernetes `<topology>-` prefix off the gNMIc target FQDN."""

    def test_strips_topology_prefix_from_gnmic_source(self):
        self.assertEqual(
            enrich.device_name_from_source(
                "atlanta-tmc-1.clabernetes.svc.cluster.local:57400"),
            "tmc-1")

    def test_hyphenated_node_name_survives(self):
        self.assertEqual(
            enrich.device_name_from_source(
                "atlanta-hub-i20e.clabernetes.svc.cluster.local:57400"),
            "hub-i20e")

    def test_bare_name_without_prefix_is_out_of_contract(self):
        """Documents a sharp edge rather than asserting it is desirable:
        the split is unconditional, so a source lacking the topology prefix
        loses its first segment. Harmless today because gNMIc always emits
        the prefixed FQDN — but it is why the `node` label is preferred."""
        self.assertEqual(enrich.device_name_from_source("hub-e:57400"), "e")


if __name__ == "__main__":
    unittest.main()
