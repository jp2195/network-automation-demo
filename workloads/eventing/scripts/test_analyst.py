"""Unit tests for the AI incident analyst lane.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with pydantic-ai-slim python3 -m unittest test_analyst -v

No model API key, no network: agent tests use TestModel/FunctionModel,
tool tests exercise only the validation layer (rejection happens before
any lazy network-dep import).
"""

import asyncio
import contextlib
import inspect
import io
import json
import unittest
from unittest import mock

import gnmi_readonly
import analyst_tools
import analyst
import postmortem
from constants import AI_ANALYSIS_MARKER
from pydantic_ai import ModelResponse, ModelRetry, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel


class TestGnmiStructurallyReadOnly(unittest.TestCase):
    """Read-only must be structural (wave-2 spec): the module exposes
    exactly one public callable, and its pygnmi usage has no Set path."""

    def test_public_surface_is_get_only(self):
        self.assertEqual(gnmi_readonly.__all__, ["get"])

    def test_no_set_call_in_source(self):
        src = inspect.getsource(gnmi_readonly)
        self.assertNotIn(".set(", src)
        self.assertNotIn("set_request", src)

    def test_rejects_node_that_could_escape_dns_suffix(self):
        for node in ("hub-e.evil.com", "hub-e/", "hub e", "HUB-E", "", "a" * 40,
                     None):
            with self.assertRaises(ValueError, msg=node):
                gnmi_readonly.get(node, "/interface[name=ethernet-1/1]")

    def test_rejects_malformed_path(self):
        for path in ("", "interface", "/interface[name=e]; rm", "/x\ny"):
            with self.assertRaises(ValueError, msg=path):
                gnmi_readonly.get("hub-e", path)


class TestToolAllowlists(unittest.TestCase):
    """Adversarial inputs must be rejected with ModelRetry BEFORE any
    network-dep import or HTTP call (no env vars are set in this suite,
    so reaching a client would raise KeyError instead — the assertions
    below would catch that as the wrong exception)."""

    def test_gnmi_get_rejects_non_srl_nodes(self):
        for node in ("fc-i20e",            # FRR cabinet — SNMP lane, not gNMI
                     "hub-e.evil.com",      # DNS-suffix escape
                     "valkey",              # arbitrary in-cluster service
                     "HUB-E", "hub e", ""):
            with self.assertRaises(ModelRetry, msg=node):
                analyst_tools.gnmi_get(node, "/interface[name=ethernet-1/1]")

    def test_gnmi_get_rejects_bad_path(self):
        for path in ("interface; drop", "interface", "", "/x\ny"):
            with self.assertRaises(ModelRetry, msg=path):
                analyst_tools.gnmi_get("hub-e", path)

    def test_snmp_get_rejects_non_cabinet_nodes(self):
        for node in ("hub-e", "fc-i20e.evil.com", "FC-I20E", ""):
            with self.assertRaises(ModelRetry, msg=node):
                asyncio.run(analyst_tools.snmp_get(node, "1.3.6.1.2.1.1.3.0"))

    def test_snmp_get_rejects_non_numeric_oid(self):
        for oid in ("sysUpTime", "1.3.6;x", "", "1"):
            with self.assertRaises(ModelRetry, msg=oid):
                asyncio.run(analyst_tools.snmp_get("fc-i20e", oid))

    def test_query_netbox_rejects_paths_outside_api(self):
        for path in ("/dcim/devices/",       # missing /api prefix
                     "/api/../admin/",        # traversal
                     "/api/dcim/?brief=1",    # query string smuggling
                     "http://evil/api/x/",    # absolute URL
                     ""):
            with self.assertRaises(ModelRetry, msg=path):
                analyst_tools.query_netbox(path)

    def test_range_minutes_clamped(self):
        # Implementation detail with a contract: a hallucinated 10-year
        # range must not become a megaquery. _clamp_minutes is module-level
        # so the bound is testable without HTTP.
        self.assertEqual(analyst_tools._clamp_minutes(999999), 360)
        self.assertEqual(analyst_tools._clamp_minutes(0), 1)
        self.assertEqual(analyst_tools._clamp_minutes("45"), 45)


_ANALYSIS_KW = dict(
    summary="Fiber cut on ring-e-i20e; IS-IS rerouted.",
    probable_root_cause="Physical layer failure on FOC-RING-EI20E",
    recommendation="Dispatch fiber crew; verify optics before restore.",
    confidence=0.85,
    evidence=[dict(source="prometheus",
                   query='link:oper_state_with_meta{link_id="ring-e-i20e"}',
                   observation="state 2 (DOWN) on both ends")],
)


class TestAgentCore(unittest.TestCase):
    def test_structured_output_with_testmodel(self):
        agent = analyst.build_agent(TestModel(call_tools=[]))
        result = agent.run_sync("analyze")
        self.assertIsInstance(result.output, analyst.IncidentAnalysis)

    def test_all_five_tool_families_registered(self):
        m = TestModel(call_tools=[])
        analyst.build_agent(m).run_sync("analyze")
        names = {t.name for t in m.last_model_request_parameters.function_tools}
        self.assertEqual(names, {"query_prometheus", "query_prometheus_range",
                                 "query_loki", "query_netbox",
                                 "gnmi_get", "snmp_get"})

    def test_tool_call_round_trip_with_functionmodel(self):
        seen = []

        def scripted(messages, info):
            if len(messages) == 1:
                return ModelResponse(parts=[
                    ToolCallPart("query_prometheus",
                                 {"promql": 'ALERTS{alertstate="firing"}'})])
            return ModelResponse(parts=[
                ToolCallPart(info.output_tools[0].name, dict(_ANALYSIS_KW))])

        def fake_prom(url, expr, timeout=10):
            seen.append(expr)
            return [{"metric": {"alertname": "SRLInterfaceOperDown"}}]

        with mock.patch.dict("os.environ", {"PROM_URL": "http://prom.test"}):
            with mock.patch("analyst_tools.prom_query", fake_prom):
                agent = analyst.build_agent(FunctionModel(scripted))
                result = agent.run_sync("analyze")
        self.assertEqual(seen, ['ALERTS{alertstate="firing"}'])
        self.assertEqual(result.output.confidence, 0.85)


class TestMarkerContract(unittest.TestCase):
    """The line analyst.py prints must round-trip through the consumer
    that already shipped in feature 3: postmortem.extract_ai_analysis."""

    def _line(self):
        a = analyst.IncidentAnalysis(**_ANALYSIS_KW)
        return analyst.render_marker_line(a, "bd056a705953f6a1")

    def test_single_line_starting_with_marker(self):
        line = self._line()
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith(AI_ANALYSIS_MARKER + " "))

    def test_fingerprint_is_stamped_not_model_supplied(self):
        # IncidentAnalysis has no fingerprint field — render_marker_line
        # injects the deterministic one from the alert.
        self.assertNotIn("fingerprint", analyst.IncidentAnalysis.model_fields)
        payload = json.loads(self._line()[len(AI_ANALYSIS_MARKER) + 1:])
        self.assertEqual(payload["fingerprint"], "bd056a705953f6a1")

    def test_postmortem_extractor_parses_it(self):
        parsed = postmortem.extract_ai_analysis([(1, self._line())])
        self.assertEqual(parsed["summary"], _ANALYSIS_KW["summary"])
        self.assertEqual(parsed["evidence"][0]["source"], "prometheus")
        md = postmortem._section_ai(parsed)
        self.assertIn("Analyst narrative", md)
        self.assertIn("Dispatch fiber crew", md)


class TestMainDegradePaths(unittest.TestCase):
    """The lane must be a no-op without the Secret, and firing-only.
    These paths must not import the OpenAI client (not installed in this
    test env — an import would error loudly, which is the point)."""

    def _run_main(self, env):
        out = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True):
            with contextlib.redirect_stdout(out):
                analyst.main()
        return out.getvalue()

    def test_secret_absent_prints_disabled_and_exits_zero(self):
        msg = self._run_main({})
        self.assertIn("AI disabled", msg)

    def test_partial_secret_counts_as_absent(self):
        msg = self._run_main({"AI_BASE_URL": "http://x:11434/v1"})
        self.assertIn("AI disabled", msg)

    def test_resolved_event_skips_analysis(self):
        body = {"alerts": [{"status": "resolved",
                            "fingerprint": "abc",
                            "labels": {"alertname": "SRLInterfaceOperDown"}}]}
        msg = self._run_main({"AI_BASE_URL": "http://x:11434/v1",
                              "AI_MODEL": "m",
                              "ALERT_JSON": json.dumps(body)})
        self.assertIn("resolved event", msg)

    def test_missing_fingerprint_skips_analysis(self):
        body = {"alerts": [{"status": "firing", "labels": {}}]}
        msg = self._run_main({"AI_BASE_URL": "http://x:11434/v1",
                              "AI_MODEL": "m",
                              "ALERT_JSON": json.dumps(body)})
        self.assertIn("no fingerprint", msg)


if __name__ == "__main__":
    unittest.main()
