"""Tests for real-space <-> hardware mapping."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from daq_xy_qt_readback.coordinate_transform import MappingSettings, map_hw_to_real, map_real_to_hw


class CoordinateTransformTests(unittest.TestCase):
    def test_identity_round_trip(self) -> None:
        settings = MappingSettings()
        vx, vy = map_real_to_hw(2.0, 8.0, settings)
        rx, ry = map_hw_to_real(vx, vy, settings)
        self.assertAlmostEqual(rx, 2.0)
        self.assertAlmostEqual(ry, 8.0)

    def test_invert_x_maps_increasing_voltage_to_left(self) -> None:
        settings = MappingSettings(invert_x=True)
        rx_low, _ = map_hw_to_real(2.0, 5.0, settings)
        rx_high, _ = map_hw_to_real(8.0, 5.0, settings)
        self.assertLess(rx_high, rx_low)

    def test_rotation_round_trip(self) -> None:
        settings = MappingSettings(rotation_enabled=True, rotation_deg=30.0)
        vx, vy = map_real_to_hw(6.0, 4.0, settings)
        rx, ry = map_hw_to_real(vx, vy, settings)
        self.assertAlmostEqual(rx, 6.0, places=6)
        self.assertAlmostEqual(ry, 4.0, places=6)


if __name__ == "__main__":
    unittest.main()

