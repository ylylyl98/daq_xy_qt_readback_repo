"""UI safety checks: initialization should not write AO outputs."""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from daq_xy_qt_readback.coordinate_transform import CoordinateSettings  # noqa: E402
from daq_xy_qt_readback.daq_xy_qt_readback import DaqXYWindow  # noqa: E402


class _FakeController:
    def __init__(self) -> None:
        self.set_output_calls = 0
        self.move_calls = 0

    def move_logical(self, x: float, y: float) -> dict[str, float]:
        self.move_calls += 1
        return {"logical_x": x, "logical_y": y, "ao0_v": x, "ao1_v": y}

    def set_output(self, ao0_v: float, ao1_v: float) -> dict[str, float]:
        self.set_output_calls += 1
        return {"logical_x": ao0_v, "logical_y": ao1_v, "ao0_v": ao0_v, "ao1_v": ao1_v}


class UiSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_ui_initialization_does_not_write_output(self) -> None:
        fake = _FakeController()
        win = DaqXYWindow(
            controller=fake,  # type: ignore[arg-type]
            initial_state={"logical_x": 3.0, "logical_y": 7.0, "ao0_v": 3.0, "ao1_v": 7.0},
            settings=CoordinateSettings.default_for_channels("ao0", "ao1"),
        )
        self.assertEqual(fake.set_output_calls, 0)
        self.assertEqual(fake.move_calls, 0)
        win.close()


if __name__ == "__main__":
    unittest.main()
