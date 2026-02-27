"""Unit tests for pure logic in daq_xy_qt_readback."""

from __future__ import annotations

import unittest
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from daq_xy_qt_readback.daq_xy_qt_readback import _clamp, _next_ramp_point


class ClampTests(unittest.TestCase):
    def test_clamp_bounds(self) -> None:
        self.assertEqual(_clamp(-1.0), 0.0)
        self.assertEqual(_clamp(11.0), 10.0)
        self.assertEqual(_clamp(5.5), 5.5)


class RampMathTests(unittest.TestCase):
    def test_next_point_lands_on_target_when_close(self) -> None:
        x, y = _next_ramp_point(1.0, 1.0, 1.02, 1.01, 0.05)
        self.assertAlmostEqual(x, 1.02)
        self.assertAlmostEqual(y, 1.01)

    def test_next_point_moves_exact_step_toward_target(self) -> None:
        x, y = _next_ramp_point(0.0, 0.0, 1.0, 0.0, 0.2)
        self.assertAlmostEqual(x, 0.2)
        self.assertAlmostEqual(y, 0.0)


if __name__ == "__main__":
    unittest.main()
