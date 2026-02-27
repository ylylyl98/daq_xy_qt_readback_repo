"""Tests for axis mapping, polarity, and rotation pipeline."""

from __future__ import annotations

import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from daq_xy_qt_readback.coordinate_transform import (  # noqa: E402
    AxisMap,
    AxisPolarity,
    CoordinateSettings,
    LogicalToPhysicalTransform,
    VoltageRange,
    logical_xy_to_physical_uv,
    physical_uv_to_ao_volts,
)


class CoordinateTransformTests(unittest.TestCase):
    def test_swapped_axis_map_routes_u_to_ao1(self) -> None:
        settings = CoordinateSettings(axis_map=AxisMap(u="ao1", v="ao0"))
        ao, _ = physical_uv_to_ao_volts(2.0, 7.0, settings)
        self.assertEqual(ao["ao1"], 2.0)
        self.assertEqual(ao["ao0"], 7.0)

    def test_inverted_polarity_maps_min_to_max_voltage(self) -> None:
        settings = CoordinateSettings(
            axis_polarity=AxisPolarity(u="inverted", v="normal"),
            voltage_range=VoltageRange(min_v=0.0, max_v=10.0),
        )
        ao, _ = physical_uv_to_ao_volts(0.0, 5.0, settings)
        self.assertEqual(ao[settings.axis_map.u], 10.0)
        self.assertEqual(ao[settings.axis_map.v], 5.0)

    def test_rotation_90deg_moves_positive_x_primarily_on_positive_v(self) -> None:
        settings = CoordinateSettings(
            transform=LogicalToPhysicalTransform(rotation_deg=90.0, rotate_about="origin")
        )
        u, v = logical_xy_to_physical_uv(1.0, 0.0, settings)
        self.assertAlmostEqual(u, 0.0, places=7)
        self.assertTrue(math.isclose(v, 1.0, rel_tol=0.0, abs_tol=1e-7))


if __name__ == "__main__":
    unittest.main()
