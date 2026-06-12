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

    def test_netbox_query_string_folds_into_params(self):
        analyst_tools._seen_calls.clear()
        captured = {}

        class FakeClient:
            def get(self, path, **params):
                captured["path"], captured["params"] = path, params
                return {"count": 0}
        with mock.patch.dict("sys.modules"):
            import netbox_client
            with mock.patch.object(netbox_client, "Client", FakeClient):
                out = analyst_tools.query_netbox("/api/dcim/cables/?label=FOC-X")
        self.assertEqual(out, {"count": 0})
        self.assertEqual(captured["path"], "/api/dcim/cables/")
        self.assertEqual(captured["params"], {"label": "FOC-X"})
        # still rejects non-API paths even with a query string
        with self.assertRaises(ModelRetry):
            analyst_tools.query_netbox("/admin/?x=1")
        analyst_tools._seen_calls.clear()

    def test_range_selector_is_stripped_from_range_queries(self):
        analyst_tools._seen_calls.clear()
        with mock.patch("analyst_tools.prom_query_range",
                        return_value=[]) as pq:
            with mock.patch.dict("os.environ", {"PROM_URL": "http://x"}):
                analyst_tools.query_prometheus_range("up{job=\"a\"}[5m]", 30)
        self.assertEqual(pq.call_args[0][1], 'up{job="a"}')
        analyst_tools._seen_calls.clear()

    def test_third_identical_call_is_blocked(self):
        analyst_tools._seen_calls.clear()
        with mock.patch("analyst_tools.prom_query", return_value=[]):
            with mock.patch.dict("os.environ", {"PROM_URL": "http://x"}):
                analyst_tools.query_prometheus("up")
                analyst_tools.query_prometheus("up")          # re-check ok
                analyst_tools.query_prometheus("up{job='x'}")  # different ok
                with self.assertRaises(ModelRetry):
                    analyst_tools.query_prometheus("up")       # 3rd → blocked
        analyst_tools._seen_calls.clear()

    def test_huge_tool_results_are_byte_bounded(self):
        big = [{"interface": f"ethernet-1/{i}", "stats": "x" * 200}
               for i in range(200)]
        out = analyst_tools._bounded(big)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["head"]), analyst_tools._MAX_RESULT_CHARS)
        small = {"oid": "1.3.6.1.2.1.1.3.0", "value": "42"}
        self.assertIs(analyst_tools._bounded(small), small)

    def test_reasoning_effort_env_wires_into_model_settings(self):
        with mock.patch.dict("os.environ", {"AI_REASONING_EFFORT": "none"}):
            self.assertEqual(
                analyst._model_settings().get("openai_reasoning_effort"), "none")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("openai_reasoning_effort", analyst._model_settings())

    def test_temperature_env_wires_into_model_settings(self):
        # Only sent when set: OpenAI reasoning-class models reject
        # non-default temperature, so there must be no default.
        with mock.patch.dict("os.environ", {"AI_TEMPERATURE": "0.2"}):
            self.assertEqual(analyst._model_settings().get("temperature"), 0.2)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("temperature", analyst._model_settings())

    def test_gnmi_get_coerces_none_response(self):
        # pygnmi yields None for empty notifications; a None tool return
        # becomes a null tool message, which Ollama's OpenAI-compat
        # endpoint rejects with HTTP 400 (smoke-found, 2026-06-12).
        with mock.patch.object(analyst_tools.gnmi_readonly, "get",
                               return_value=None):
            out = analyst_tools.gnmi_get("hub-e", "/interface[name=ethernet-1/1]")
        self.assertEqual(out, {"error": "empty gNMI response"})

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
                     "/admin/?next=/api/",    # query string can't launder a bad path
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
