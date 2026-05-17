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


if __name__ == "__main__":
    unittest.main()
