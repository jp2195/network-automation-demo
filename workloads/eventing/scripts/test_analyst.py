"""Unit tests for the AI incident analyst lane.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with pydantic-ai-slim python3 -m unittest test_analyst -v

No model API key, no network: agent tests use TestModel/FunctionModel,
tool tests exercise only the validation layer (rejection happens before
any lazy network-dep import).
"""

import inspect
import unittest

import gnmi_readonly


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


if __name__ == "__main__":
    unittest.main()
