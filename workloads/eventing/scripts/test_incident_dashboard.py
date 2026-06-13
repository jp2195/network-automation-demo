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


if __name__ == "__main__":
    unittest.main()
