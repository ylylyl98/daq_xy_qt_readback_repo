"""Restart safeguard checks with fake controller backend."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from daq_xy_qt_readback.coordinate_transform import (  # noqa: E402
    CoordinateSettings,
    coordinate_settings_from_dict,
)
from daq_xy_qt_readback.daq_xy_qt_readback import DaqXYWindow  # noqa: E402
from daq_xy_qt_readback.scanner_controller import ScannerController  # noqa: E402


class _ControllerAdapter:
    def __init__(self, controller: ScannerController) -> None:
        self.controller = controller
        self.move_calls = 0

    def move_logical(self, x: float, y: float) -> dict[str, float]:
        self.move_calls += 1
        return self.controller.move_logical(x, y)


class RestartSafeguardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_restart_reopen_keeps_same_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ctrl = ScannerController(
                dev_name="Dev1",
                ao_x="ao0",
                ao_y="ao1",
                data_dir=pathlib.Path(td),
                fake_backend=True,
            )
            ctrl.set_output(3.0, 7.0)
            before = ctrl.get_state()
            settings = coordinate_settings_from_dict(
                ctrl.get_config(),
                CoordinateSettings.default_for_channels("ao0", "ao1"),
            )
            adapter = _ControllerAdapter(ctrl)

            win1 = DaqXYWindow(controller=adapter, initial_state=before, settings=settings)  # type: ignore[arg-type]
            win1.close()
            mid = ctrl.get_state()

            win2 = DaqXYWindow(controller=adapter, initial_state=mid, settings=settings)  # type: ignore[arg-type]
            win2.close()
            after = ctrl.get_state()

            self.assertEqual(adapter.move_calls, 0)
            self.assertAlmostEqual(before["ao0_v"], mid["ao0_v"])
            self.assertAlmostEqual(before["ao1_v"], mid["ao1_v"])
            self.assertAlmostEqual(mid["ao0_v"], after["ao0_v"])
            self.assertAlmostEqual(mid["ao1_v"], after["ao1_v"])


if __name__ == "__main__":
    unittest.main()
