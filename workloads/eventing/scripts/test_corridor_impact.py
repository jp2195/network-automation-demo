"""Unit tests for the corridor what-if reachability walk (chat lane).

Run: cd workloads/eventing/scripts && python3 -m unittest test_corridor_impact -v

No network: compute() is pure over NetBox-shaped cable + device records
(mirroring spec/atlanta.yaml and the seeded /api/dcim/ responses); the
fetching wrapper is tested with a stubbed fetch.
"""

import unittest
from unittest import mock

import corridor_impact


# The 15 links of spec/atlanta.yaml as (label, corridor, node_a, node_b).
# Kept in lockstep with the spec on purpose — the assertions below encode
# the real demo answers.
_CABLES = [
    ("FOC-RING-NE", "I-285", "hub-n", "hub-e"),
    ("FOC-RING-EI20E", "I-285", "hub-e", "hub-i20e"),
    ("FOC-RING-I20ESW", "I-285", "hub-i20e", "hub-sw"),
    ("FOC-RING-SWI20W", "I-285", "hub-sw", "hub-i20w"),
    ("FOC-RING-I20WNW", "I-285", "hub-i20w", "hub-nw"),
    ("FOC-RING-NWN", "I-285", "hub-nw", "hub-n"),
    ("FOC-NW-01", "I-75 NW", "tmc-1", "hub-nw"),
    ("FOC-SW-01", "I-85 SW", "tmc-1", "hub-sw"),
    ("FOC-N-01", "GA-400", "tmc-2", "hub-n"),
    ("FOC-E-01", "I-285 East", "tmc-2", "hub-e"),
    ("FOC-TMC-XC", "TMC Interconnect", "tmc-1", "tmc-2"),
    ("FOC-CAB-N", "GA-400", "hub-n", "fc-n"),
    ("FOC-CAB-NW", "I-75 NW", "hub-nw", "fc-nw"),
    ("FOC-CAB-I20E", "I-20 East", "hub-i20e", "fc-i20e"),
    ("FOC-CAB-SW", "I-85 SW", "hub-sw", "fc-sw"),
]

_ROLES = {"tmc-1": "tmc", "tmc-2": "tmc",
          "fc-n": "field-cabinet", "fc-nw": "field-cabinet",
          "fc-i20e": "field-cabinet", "fc-sw": "field-cabinet"}


def _term(device):
    # Shape of one entry in a_terminations/b_terminations on
    # /api/dcim/cables/ (NetBox 4.x): the peer interface inline.
    return [{"object_type": "dcim.interface",
             "object": {"device": {"name": device}, "name": "ethernet-1/1"}}]


def _cables():
    return [{"label": label,
             "custom_fields": {"corridor": corridor},
             "a_terminations": _term(a), "b_terminations": _term(b)}
            for label, corridor, a, b in _CABLES]


def _devices():
    """All routers, plus fc-n's ITS roster (as seeded at its site)."""
    devs = []
    routers = sorted({n for _, _, a, b in _CABLES for n in (a, b)})
    for name in routers:
        role = _ROLES.get(name, "corridor-hub")
        tags = []
        if name == "fc-n":
            tags = [{"slug": "northridge-transportation-authority"},
                    {"slug": "adot-region-7"}]
        devs.append({"name": name, "role": {"slug": role},
                     "site": {"slug": name + "-site"}, "tags": tags})
    for i, role in enumerate(["cctv-camera", "cctv-camera",
                              "signal-controller"]):
        devs.append({"name": f"fc-n-asset-{i}", "role": {"slug": role},
                     "site": {"slug": "fc-n-site"}, "tags": []})
    # An ITS asset at a NON-isolated site must never be counted.
    devs.append({"name": "fc-sw-cctv-01", "role": {"slug": "cctv-camera"},
                 "site": {"slug": "fc-sw-site"}, "tags": []})
    return devs


class CorridorMatchTest(unittest.TestCase):
    def test_loose_spelling_matches_corridor_family(self):
        # "I285" (how people type it) must match both "I-285" (the ring)
        # and "I-285 East" (the tmc-2 spur along the same highway).
        got = corridor_impact.match_corridors(
            "I285", {"I-285", "I-285 East", "I-20 East", "GA-400"})
        self.assertEqual(got, {"I-285", "I-285 East"})

    def test_exact_name_matches_only_itself(self):
        got = corridor_impact.match_corridors(
            "ga-400", {"I-285", "GA-400", "I-20 East"})
        self.assertEqual(got, {"GA-400"})

    def test_no_match_is_empty(self):
        self.assertEqual(
            corridor_impact.match_corridors("I-95", {"I-285", "GA-400"}),
            set())


class ReachabilityTest(unittest.TestCase):
    def test_ga400_cut_isolates_only_its_field_cabinet(self):
        # GA-400 carries the tmc-2 uplink + the fc-n cabinet drop. hub-n
        # survives via the ring; single-homed fc-n goes dark.
        out = corridor_impact.compute("GA-400", _cables(), _devices())
        self.assertEqual(out["matched_corridors"], ["GA-400"])
        self.assertEqual(
            sorted(l["cable_label"] for l in out["links_cut"]),
            ["FOC-CAB-N", "FOC-N-01"])
        self.assertEqual(
            [d["device"] for d in out["isolated_devices"]], ["fc-n"])

    def test_i285_cut_isolates_far_side_of_ring(self):
        # All 6 ring cables + the I-285 East spur go: hub-e loses every
        # path; hub-i20e and hub-i20w have no TMC uplink of their own;
        # fc-i20e hangs off hub-i20e. hub-n/hub-nw/hub-sw survive via
        # their TMC uplinks.
        out = corridor_impact.compute("I285", _cables(), _devices())
        self.assertEqual(out["matched_corridors"], ["I-285", "I-285 East"])
        self.assertEqual(
            sorted(d["device"] for d in out["isolated_devices"]),
            ["fc-i20e", "hub-e", "hub-i20e", "hub-i20w"])

    def test_unknown_corridor_reports_available_names(self):
        out = corridor_impact.compute("I-95", _cables(), _devices())
        self.assertIn("error", out)
        self.assertIn("GA-400", out["available_corridors"])

    def test_isolating_a_cabinet_is_high_severity(self):
        out = corridor_impact.compute("GA-400", _cables(), _devices())
        self.assertEqual(out["severity_class"], "high")

    def test_no_isolation_is_low_severity(self):
        # The TMC interconnect alone strands nobody (both TMCs are sources).
        out = corridor_impact.compute("TMC Interconnect",
                                      _cables(), _devices())
        self.assertEqual(out["isolated_devices"], [])
        self.assertEqual(out["severity_class"], "low")

    def test_agencies_and_its_assets_from_isolated_sites_only(self):
        out = corridor_impact.compute("GA-400", _cables(), _devices())
        self.assertEqual(out["affected_agencies"],
                         ["adot-region-7",
                          "northridge-transportation-authority"])
        # fc-n's roster only — fc-sw's camera stays connected.
        self.assertEqual(out["its_assets_lost"],
                         {"cctv-camera": 2, "signal-controller": 1})


class FetchWrapperTest(unittest.TestCase):
    def test_netbox_failure_returns_error_not_raise(self):
        with mock.patch.object(corridor_impact, "_fetch",
                               side_effect=OSError("netbox unreachable")):
            out = corridor_impact.corridor_impact("GA-400")
        self.assertIn("netbox", out["error"].lower())

    def test_wrapper_feeds_compute(self):
        with mock.patch.object(corridor_impact, "_fetch",
                               return_value=(_cables(), _devices())):
            out = corridor_impact.corridor_impact("GA-400")
        self.assertEqual([d["device"] for d in out["isolated_devices"]],
                         ["fc-n"])


if __name__ == "__main__":
    unittest.main()
