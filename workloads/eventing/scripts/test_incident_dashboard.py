"""Unit tests for the per-incident Grafana dashboard lane.

No cluster needed: k8s_api's HTTP layer is monkeypatched; everything
else is pure JSON construction.
"""

import json
import unittest
from unittest import mock

import incident_dashboard
import k8s_api


class TestK8sApi(unittest.TestCase):
    def test_create_configmap_request_shape(self):
        sent = {}

        def fake_request(method, path, body=None):
            sent["method"], sent["path"], sent["body"] = method, path, body
            return 201, {}

        with mock.patch.object(k8s_api, "_request", fake_request):
            k8s_api.create_configmap(
                "monitoring", "incident-abc123",
                data={"incident-abc123.json": "{}"},
                labels={"grafana_dashboard": "1"},
                annotations={"grafana_folder": "Incidents"})
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/api/v1/namespaces/monitoring/configmaps")
        self.assertEqual(sent["body"]["metadata"]["name"], "incident-abc123")
        self.assertEqual(sent["body"]["metadata"]["labels"],
                         {"grafana_dashboard": "1"})
        self.assertEqual(sent["body"]["metadata"]["annotations"],
                         {"grafana_folder": "Incidents"})

    def test_create_conflict_is_replaced(self):
        # 409 → delete + retry once (idempotent re-fire of the same incident)
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path))
            if method == "POST" and len([c for c in calls if c[0] == "POST"]) == 1:
                return 409, {}
            return 200, {}

        with mock.patch.object(k8s_api, "_request", fake_request):
            k8s_api.create_configmap("monitoring", "incident-x",
                                     data={}, labels={}, annotations={})
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["POST", "DELETE", "POST"])

    def test_delete_configmap_404_is_ok(self):
        with mock.patch.object(k8s_api, "_request",
                               lambda m, p, body=None: (404, {})):
            # must not raise — resolve after a lost firing is normal
            k8s_api.delete_configmap("monitoring", "incident-gone")

    def test_delete_other_error_raises(self):
        with mock.patch.object(k8s_api, "_request",
                               lambda m, p, body=None: (500, {"reason": "x"})):
            with self.assertRaises(RuntimeError):
                k8s_api.delete_configmap("monitoring", "incident-x")


_ENRICHMENT = {
    "alert": {"name": "SRLInterfaceOperDown", "status": "firing",
              "fingerprint": "AB12cd34!", "link_id": "hubi20e-fci20e",
              "started": "2026-06-13T00:00:00Z"},
    "device": {"name": "hub-i20e"},
    "site": {"slug": "lithonia", "name": "Lithonia"},
    "cable": {"label": "FOC-CAB-I20E", "provider": "ADOT-owned fiber",
              "corridor": "I-20 East", "sla": "8h",
              "circuit_id": "ADOT-CAB-I20E-303"},
}
_IMPACT = {
    "affected_device": "hub-i20e", "site_slug": "lithonia",
    "downstream_devices": [
        {"device": "hub-e", "interface": "ethernet-1/1",
         "cable_label": "FOC-RING-EI20E"},
        {"device": "fc-i20e", "interface": "eth1",
         "cable_label": "FOC-CAB-I20E"},
    ],
    "affected_agencies": ["adot-region-7"], "severity_class": "high",
}


class TestBuildDashboard(unittest.TestCase):
    def setUp(self):
        self.fp = incident_dashboard.safe_fp(_ENRICHMENT["alert"]["fingerprint"])
        self.dash = incident_dashboard.build_dashboard(
            _ENRICHMENT, _IMPACT, self.fp)

    def test_fp_sanitized_and_lowercased(self):
        self.assertEqual(self.fp, "ab12cd34")

    def test_identity(self):
        self.assertEqual(self.dash["uid"], "incident-ab12cd34")
        self.assertIn("SRLInterfaceOperDown", self.dash["title"])
        self.assertIn("hub-i20e", self.dash["title"])
        self.assertEqual(self.dash["schemaVersion"], 38)
        self.assertIn("incident", self.dash["tags"])

    def test_link_panels_target_the_link_id(self):
        text = json.dumps(self.dash["panels"])
        self.assertIn('link:oper_state_with_meta{link_id=\\"hubi20e-fci20e\\"}',
                      text)
        self.assertIn('link:rate_in_bps:1m{link_id=\\"hubi20e-fci20e\\"}', text)

    def test_downstream_grid_split_by_kind(self):
        text = json.dumps(self.dash["panels"])
        # SRL downstream → oper-state stat
        self.assertIn('srl_nokia_interfaces_interface_oper_state{node=\\"hub-e\\"'
                      ', interface=\\"ethernet-1/1\\"}', text)
        # cabinet downstream → SNMP reachability stat
        self.assertIn('up{job=\\"snmp-frr-cabinets\\", node=\\"fc-i20e\\"}', text)

    def test_ai_panel_pins_marker_and_fp(self):
        text = json.dumps(self.dash["panels"])
        self.assertIn("INCIDENT_ANALYSIS_V1 {", text)
        self.assertIn("ab12cd34", text)

    def test_context_panel_carries_cable_facts(self):
        text = json.dumps(self.dash["panels"])
        for needle in ("FOC-CAB-I20E", "I-20 East", "8h", "adot-region-7",
                       "high"):
            self.assertIn(needle, text)

    def test_no_link_id_skips_link_panels_but_builds(self):
        enr = json.loads(json.dumps(_ENRICHMENT))
        enr["alert"]["link_id"] = None
        dash = incident_dashboard.build_dashboard(enr, _IMPACT, self.fp)
        self.assertNotIn("link:oper_state_with_meta", json.dumps(dash["panels"]))
        self.assertTrue(dash["panels"])

    def test_grid_positions_do_not_overlap(self):
        seen = set()
        for p in self.dash["panels"]:
            g = p["gridPos"]
            key = (g["x"], g["y"])
            self.assertNotIn(key, seen)
            seen.add(key)


class TestMainBranches(unittest.TestCase):
    def _run(self, status, fp="abc123"):
        enr = json.loads(json.dumps(_ENRICHMENT))
        enr["alert"]["status"] = status
        enr["alert"]["fingerprint"] = fp
        calls = []
        with mock.patch.object(incident_dashboard.k8s_api, "create_configmap",
                               lambda *a, **k: calls.append(("create", a, k))):
            with mock.patch.object(incident_dashboard.k8s_api,
                                   "delete_configmap",
                                   lambda *a, **k: calls.append(("delete", a, k))):
                with mock.patch.dict("os.environ", {
                        "ENRICHMENT_JSON": json.dumps(enr),
                        "IMPACT_JSON": json.dumps(_IMPACT)}):
                    incident_dashboard.main()
        return calls

    def test_firing_creates_labeled_cm(self):
        calls = self._run("firing")
        self.assertEqual(len(calls), 1)
        kind, args, kwargs = calls[0]
        self.assertEqual(kind, "create")
        self.assertEqual(args[0], "monitoring")
        self.assertEqual(args[1], "incident-abc123")
        self.assertEqual(kwargs["labels"], {"grafana_dashboard": "1"})
        self.assertEqual(kwargs["annotations"], {"grafana_folder": "Incidents"})
        body = json.loads(kwargs["data"]["incident-abc123.json"])
        self.assertEqual(body["uid"], "incident-abc123")

    def test_resolved_deletes_cm(self):
        calls = self._run("resolved")
        self.assertEqual(calls, [("delete", ("monitoring", "incident-abc123"),
                                  {})])

    def test_no_fingerprint_is_noop(self):
        calls = self._run("firing", fp="!!!")
        self.assertEqual(calls, [])

    def test_api_failure_does_not_raise(self):
        # advisory surface: a dashboard hiccup must not fail the pipeline
        enr = json.loads(json.dumps(_ENRICHMENT))
        def boom(*a, **k):
            raise RuntimeError("api down")
        with mock.patch.object(incident_dashboard.k8s_api,
                               "create_configmap", boom):
            with mock.patch.dict("os.environ", {
                    "ENRICHMENT_JSON": json.dumps(enr),
                    "IMPACT_JSON": json.dumps(_IMPACT)}):
                incident_dashboard.main()  # must not raise


if __name__ == "__main__":
    unittest.main()
