"""Unit tests for pure logic in daq_xy_qt_readback."""

from __future__ import annotations

import os
import unittest
import pathlib
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtWidgets import QApplication

import daq_xy_qt_readback.daq_xy_qt_readback as ui_mod
from daq_xy_qt_readback.coordinate_transform import MappingSettings
from daq_xy_qt_readback.daq_xy_qt_readback import _clamp, _next_ramp_point


class _FakeTask:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeDaqControl:
    initial_outputs = {"ao0": 0.0, "ao1": 0.0}
    fail_readback = False

    def __init__(self, device_name: str) -> None:
        self.device_name = device_name
        self.ai_task = _FakeTask()
        self.ao_task = _FakeTask()
        self.ai_index = 0
        self.ao_index = 0
        self.x_indexes: dict[str, int] = {}
        self.y_indexes: dict[str, int] = {}
        self.x_values: list[float] = []
        self.y_values: list[float] = []
        self._ordered_addresses: list[str] = []
        self.hardware = {k: float(v) for k, v in type(self).initial_outputs.items()}
        self.fail_readback = bool(type(self).fail_readback)
        self.write_calls: list[tuple[float, ...]] = []

    def add_ao_channel(self, address: str, variable: str) -> None:
        measured = float(self.hardware[address])
        self.x_indexes[variable] = self.ao_index
        self.y_indexes["measured_" + variable] = self.ai_index
        self.x_values.append(measured)
        self.y_values.append(measured)
        self._ordered_addresses.append(address)
        self.ai_index += 1
        self.ao_index += 1

    def receive_x(self, variable: str, value: float) -> None:
        self.x_values[self.x_indexes[variable]] = float(value)

    def write_x(self) -> None:
        self.write_calls.append(tuple(float(v) for v in self.x_values))
        for address, value in zip(self._ordered_addresses, self.x_values):
            self.hardware[address] = float(value)
        self.read_y()

    def read_y(self) -> None:
        if self.fail_readback:
            raise RuntimeError("AO readback unavailable")
        self.y_values = [float(self.hardware[address]) for address in self._ordered_addresses]

    def send_y(self, variable: str) -> float:
        if self.fail_readback:
            raise RuntimeError("AO readback unavailable")
        return float(self.y_values[self.y_indexes[variable]])


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


class DaqInterfaceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 0.0, "ao1": 0.0}
        FakeDaqControl.fail_readback = False
        self._patcher = mock.patch.object(ui_mod, "_RealDaqControl", FakeDaqControl)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_init_preserves_existing_outputs_without_write(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 3.25, "ao1": 6.5}

        daq = ui_mod.DaqInterface("Dev1", "ao0", "ao1")

        self.assertAlmostEqual(daq._vx, 3.25)
        self.assertAlmostEqual(daq._vy, 6.5)
        self.assertFalse(daq.readback_uncertain)
        self.assertEqual(daq._daq.write_calls, [])

    def test_init_uses_preserved_driver_cache_if_readback_is_unavailable(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 1.5, "ao1": 8.75}
        FakeDaqControl.fail_readback = True

        daq = ui_mod.DaqInterface("Dev1", "ao0", "ao1")

        self.assertAlmostEqual(daq._vx, 1.5)
        self.assertAlmostEqual(daq._vy, 8.75)
        self.assertTrue(daq.readback_uncertain)
        self.assertIn("No startup write was issued", daq.readback_status)
        self.assertEqual(daq._daq.write_calls, [])

    def test_close_does_not_write_outputs(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 2.0, "ao1": 4.0}

        daq = ui_mod.DaqInterface("Dev1", "ao0", "ao1")
        daq.close()

        self.assertEqual(daq._daq.write_calls, [])
        self.assertTrue(daq._daq.ao_task.closed)
        self.assertTrue(daq._daq.ai_task.closed)

    def test_explicit_write_is_the_only_operation_that_changes_outputs(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 2.0, "ao1": 4.0}

        daq = ui_mod.DaqInterface("Dev1", "ao0", "ao1")
        daq.write_outputs(2.5, 4.5)

        self.assertEqual(daq._daq.write_calls, [(2.5, 4.5)])
        self.assertAlmostEqual(daq._daq.hardware["ao0"], 2.5)
        self.assertAlmostEqual(daq._daq.hardware["ao1"], 4.5)


class WindowSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeDaqControl.initial_outputs = {"ao0": 7.0, "ao1": 1.25}
        FakeDaqControl.fail_readback = False
        self._patcher = mock.patch.object(ui_mod, "_RealDaqControl", FakeDaqControl)
        self._patcher.start()
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_window_initializes_from_existing_hardware_outputs(self) -> None:
        win = ui_mod.DaqXYWindow(
            dev_name="Dev1",
            ao_x="ao0",
            ao_y="ao1",
            mapping=MappingSettings(),
            devices=["Dev1"],
            channels_by_device={"Dev1": ["ao0", "ao1"]},
            demo_reason=None,
        )
        try:
            self.assertAlmostEqual(win._vx, 7.0)
            self.assertAlmostEqual(win._vy, 1.25)
            self.assertIn("readback=measured", win.lbl_status.text())
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_apply_mapping_save_failure_keeps_outputs_unchanged_and_ramp_stopped(self) -> None:
        win = ui_mod.DaqXYWindow(
            dev_name="Dev1",
            ao_x="ao0",
            ao_y="ao1",
            mapping=MappingSettings(),
            devices=["Dev1", "Dev2"],
            channels_by_device={"Dev1": ["ao0", "ao1"], "Dev2": ["ao0", "ao1"]},
            demo_reason=None,
        )
        try:
            old_daq = win._daq
            win.chk_enable.setChecked(True)
            win._set_target_hw(9.0, 2.5)
            self.assertTrue(win._ramp_timer.isActive())

            win.cmb_device.setCurrentText("Dev2")
            win.cmb_x_ch.setCurrentText("ao0")
            win.cmb_y_ch.setCurrentText("ao1")

            with mock.patch.object(ui_mod, "_save_persisted_mapping", side_effect=OSError("disk full")):
                win._on_apply_mapping()

            self.assertEqual(win._selected_device, "Dev2")
            self.assertEqual(win._daq.dev_name, "Dev2")
            self.assertFalse(win._ramp_timer.isActive())
            self.assertAlmostEqual(win._target_vx, win._vx)
            self.assertAlmostEqual(win._target_vy, win._vy)
            self.assertEqual(old_daq._daq.write_calls, [])
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_failed_reconnect_restores_previous_ramp_without_writing(self) -> None:
        win = ui_mod.DaqXYWindow(
            dev_name="Dev1",
            ao_x="ao0",
            ao_y="ao1",
            mapping=MappingSettings(),
            devices=["Dev1", "Dev2"],
            channels_by_device={"Dev1": ["ao0", "ao1"], "Dev2": ["ao0", "ao1"]},
            demo_reason=None,
        )
        try:
            old_daq = win._daq
            win.chk_enable.setChecked(True)
            win._set_target_hw(9.0, 2.5)
            self.assertTrue(win._ramp_timer.isActive())

            win.cmb_device.setCurrentText("Dev2")
            win.cmb_x_ch.setCurrentText("ao0")
            win.cmb_y_ch.setCurrentText("ao1")

            with (
                mock.patch.object(ui_mod, "DaqInterface", side_effect=RuntimeError("connect failed")),
                mock.patch.object(ui_mod.QMessageBox, "warning"),
            ):
                win._on_apply_mapping()

            self.assertIs(win._daq, old_daq)
            self.assertEqual(win._selected_device, "Dev1")
            self.assertTrue(win._ramp_timer.isActive())
            self.assertEqual(old_daq._daq.write_calls, [])
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
