"""Unit tests for the AI incident analyst lane.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with pydantic-ai-slim python3 -m unittest test_analyst -v

No model API key, no network: agent tests use TestModel/FunctionModel,
tool tests exercise only the validation layer (rejection happens before
any lazy network-dep import).
"""

import asyncio
import inspect
import unittest

import gnmi_readonly
import analyst_tools
from pydantic_ai import ModelRetry


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
        with self.assertRaises(ModelRetry):
            analyst_tools.gnmi_get("hub-e", "interface; drop")

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


if __name__ == "__main__":
    unittest.main()
