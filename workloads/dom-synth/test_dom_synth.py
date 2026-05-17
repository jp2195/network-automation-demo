"""Unit tests for dom_synth gray-failure logic."""

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import dom_synth  # noqa: E402


class RampTests(unittest.TestCase):
    """Piecewise-linear ramp:
        [0,     0.20*d) -> linear 0 -> 1.0
        [0.20*d, 0.80*d) -> 1.0 (plateau)
        [0.80*d, 1.00*d] -> linear 1.0 -> 0
        outside [0, d]   -> 0
    """

    def _gf(self, start=1000.0, duration=100.0):
        return dom_synth.GrayFailure(
            link_id="ring-n-e",
            start_ts=start,
            duration_s=duration,
            peak_rx_offset_dbm=8.0,
            peak_errors_per_sec=120.0,
        )

    def test_before_start_is_zero(self):
        self.assertAlmostEqual(dom_synth.ramp(990.0, self._gf()), 0.0)

    def test_start_is_zero(self):
        self.assertAlmostEqual(dom_synth.ramp(1000.0, self._gf()), 0.0)

    def test_mid_ramp_up_is_half(self):
        # t/d = 0.10 -> half of ramp-up phase (which spans 0..0.20)
        self.assertAlmostEqual(dom_synth.ramp(1010.0, self._gf()), 0.5, places=4)

    def test_end_of_ramp_up_is_one(self):
        self.assertAlmostEqual(dom_synth.ramp(1020.0, self._gf()), 1.0, places=4)

    def test_plateau_is_one(self):
        self.assertAlmostEqual(dom_synth.ramp(1050.0, self._gf()), 1.0, places=4)

    def test_start_of_ramp_down_is_one(self):
        self.assertAlmostEqual(dom_synth.ramp(1080.0, self._gf()), 1.0, places=4)

    def test_mid_ramp_down_is_half(self):
        # t/d = 0.90 -> half of ramp-down phase (0.80..1.00)
        self.assertAlmostEqual(dom_synth.ramp(1090.0, self._gf()), 0.5, places=4)

    def test_end_is_zero(self):
        self.assertAlmostEqual(dom_synth.ramp(1100.0, self._gf()), 0.0, places=4)

    def test_after_end_is_zero(self):
        self.assertAlmostEqual(dom_synth.ramp(1150.0, self._gf()), 0.0)


class ParseGrayFailureTests(unittest.TestCase):
    def test_valid_json_returns_dataclass(self):
        raw = json.dumps({
            "start_ts": 1747500000,
            "duration_s": 600,
            "peak_rx_offset_dbm": 8.0,
            "peak_errors_per_sec": 120,
        })
        gf = dom_synth.parse_gray_failure("ring-n-e", raw)
        self.assertIsNotNone(gf)
        self.assertEqual(gf.link_id, "ring-n-e")
        self.assertEqual(gf.start_ts, 1747500000)
        self.assertEqual(gf.duration_s, 600)
        self.assertEqual(gf.peak_rx_offset_dbm, 8.0)
        self.assertEqual(gf.peak_errors_per_sec, 120)

    def test_malformed_json_returns_none(self):
        self.assertIsNone(dom_synth.parse_gray_failure("ring-n-e", "not-json"))

    def test_missing_field_returns_none(self):
        raw = json.dumps({"start_ts": 1, "duration_s": 60})  # missing two fields
        self.assertIsNone(dom_synth.parse_gray_failure("ring-n-e", raw))

    def test_zero_duration_returns_none(self):
        raw = json.dumps({
            "start_ts": 1, "duration_s": 0,
            "peak_rx_offset_dbm": 1.0, "peak_errors_per_sec": 1.0,
        })
        self.assertIsNone(dom_synth.parse_gray_failure("ring-n-e", raw))

    def test_bytes_input_is_accepted(self):
        # Valkey returns bytes by default; the function must accept either.
        raw = json.dumps({
            "start_ts": 1, "duration_s": 60,
            "peak_rx_offset_dbm": 1.0, "peak_errors_per_sec": 1.0,
        }).encode()
        gf = dom_synth.parse_gray_failure("ring-n-e", raw)
        self.assertIsNotNone(gf)


class LoadGrayFailuresTests(unittest.TestCase):
    def setUp(self):
        import fakeredis
        self.fake = fakeredis.FakeRedis()

    def _set(self, link_id, **kwargs):
        defaults = {
            "start_ts": 1747500000,
            "duration_s": 600,
            "peak_rx_offset_dbm": 8.0,
            "peak_errors_per_sec": 120,
        }
        defaults.update(kwargs)
        self.fake.set(f"gray:{link_id}", json.dumps(defaults))

    def test_empty_db_returns_empty(self):
        result = dom_synth._load_gray_failures(client=self.fake)
        self.assertEqual(result, {})

    def test_single_key_returns_one_entry(self):
        self._set("ring-n-e")
        result = dom_synth._load_gray_failures(client=self.fake)
        self.assertEqual(set(result.keys()), {"ring-n-e"})
        self.assertEqual(result["ring-n-e"].peak_rx_offset_dbm, 8.0)

    def test_two_keys_return_two_entries(self):
        self._set("ring-n-e")
        self._set("ring-e-i20e", peak_rx_offset_dbm=4.0)
        result = dom_synth._load_gray_failures(client=self.fake)
        self.assertEqual(set(result.keys()), {"ring-n-e", "ring-e-i20e"})
        self.assertEqual(result["ring-e-i20e"].peak_rx_offset_dbm, 4.0)

    def test_malformed_value_is_skipped_others_kept(self):
        self.fake.set("gray:ring-n-e", "not-json")
        self._set("ring-e-i20e")
        result = dom_synth._load_gray_failures(client=self.fake)
        self.assertEqual(set(result.keys()), {"ring-e-i20e"})

    def test_connection_error_returns_empty(self):
        # Simulate a broken client by passing one whose .keys() raises.
        class BrokenClient:
            def keys(self, *_a, **_kw):
                import valkey
                raise valkey.ConnectionError("simulated")
            def get(self, *_a, **_kw):
                raise AssertionError("should not be reached")
        result = dom_synth._load_gray_failures(client=BrokenClient())
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
