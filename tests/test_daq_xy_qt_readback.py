"""Unit tests for pure logic in daq_xy_qt_readback."""

from __future__ import annotations

import os
import unittest
import pathlib
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtTest import QTest
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


class WindowGeometryTests(unittest.TestCase):
    def test_centered_top_left_uses_available_screen_geometry(self) -> None:
        available = QRect(0, 40, 1920, 1040)

        position = ui_mod._centered_top_left(available, QSize(190, 190))

        self.assertEqual(position, QPoint(865, 465))

    def test_centered_top_left_supports_negative_monitor_coordinates(self) -> None:
        available = QRect(-1920, -120, 1920, 1080)

        position = ui_mod._centered_top_left(available, QSize(1180, 720))

        self.assertEqual(position, QPoint(-1550, 60))


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
        self._positioner_patcher = mock.patch.object(
            ui_mod,
            "load_positioner_settings",
            return_value=ui_mod.PositionerSettings(enabled=False),
        )
        self._positioner_patcher.start()
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        self._positioner_patcher.stop()
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
            self.assertTrue(win.chk_enable.isEnabled())
            self.assertFalse(win.btn_positioner_connect.isEnabled())
            self.assertEqual(win.lbl_positioner_status.text(), "Not configured")
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

    def test_closing_during_ramp_preserves_last_actual_outputs(self) -> None:
        win = ui_mod.DaqXYWindow(
            dev_name="Dev1",
            ao_x="ao0",
            ao_y="ao1",
            mapping=MappingSettings(),
            devices=["Dev1"],
            channels_by_device={"Dev1": ["ao0", "ao1"]},
            demo_reason=None,
        )
        win.chk_enable.setChecked(True)
        win._set_target_hw(9.0, 2.5)
        self.assertTrue(win._ramp_timer.isActive())
        outputs_before_close = dict(win._daq._daq.hardware)
        writes_before_close = list(win._daq._daq.write_calls)

        win.close()

        self.assertFalse(win._ramp_timer.isActive())
        self.assertEqual(win._daq._daq.hardware, outputs_before_close)
        self.assertEqual(win._daq._daq.write_calls, writes_before_close)

    def test_compact_mode_switch_preserves_state_and_does_not_write(self) -> None:
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
            win.setGeometry(80, 90, 900, 600)
            win.show()
            self.app.processEvents()
            full_geometry = win.geometry()
            win.chk_enable.setChecked(True)
            original_target = (win._target_rx, win._target_ry)

            win._enter_compact_mode()
            self.app.processEvents()

            self.assertTrue(win._compact_mode)
            self.assertIs(win._view_stack.currentWidget(), win._compact_page)
            self.assertEqual(win.size(), ui_mod.QSize(300, 360))
            screen_center = win.screen().availableGeometry().center()
            compact_center = win.frameGeometry().center()
            self.assertLessEqual(abs(compact_center.x() - screen_center.x()), 3)
            self.assertLessEqual(abs(compact_center.y() - screen_center.y()), 3)
            self.assertTrue(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertTrue(win._enabled)
            self.assertEqual((win._target_rx, win._target_ry), original_target)
            self.assertEqual(win._daq._daq.write_calls, [])

            win._exit_compact_mode()
            self.app.processEvents()

            self.assertFalse(win._compact_mode)
            self.assertIs(win._view_stack.currentWidget(), win._full_page)
            self.assertEqual(win.size(), full_geometry.size())
            screen_center = win.screen().availableGeometry().center()
            window_center = win.frameGeometry().center()
            self.assertLessEqual(abs(window_center.x() - screen_center.x()), 3)
            self.assertLessEqual(abs(window_center.y() - screen_center.y()), 3)
            self.assertFalse(win.isMaximized())
            self.assertFalse(win.isFullScreen())
            self.assertTrue(win._enabled)
            self.assertEqual((win._target_rx, win._target_ry), original_target)
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_compact_buttons_nudge_in_all_real_space_directions(self) -> None:
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
            win.chk_enable.setChecked(True)
            win._enter_compact_mode()
            start_x, start_y = win._target_rx, win._target_ry

            win.compact_btn_left.click()
            self.assertAlmostEqual(win._target_rx, start_x - ui_mod.STEP_PER_MOVE)
            self.assertAlmostEqual(win._target_ry, start_y)
            win.compact_btn_right.click()
            self.assertAlmostEqual(win._target_rx, start_x)
            self.assertAlmostEqual(win._target_ry, start_y)
            win.compact_btn_up.click()
            self.assertAlmostEqual(win._target_rx, start_x)
            self.assertAlmostEqual(win._target_ry, start_y + ui_mod.STEP_PER_MOVE)
            win.compact_btn_down.click()
            self.assertAlmostEqual(win._target_rx, start_x)
            self.assertAlmostEqual(win._target_ry, start_y)
        finally:
            win.close()

    def test_expand_after_maximized_window_restores_normal_size(self) -> None:
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
            win.resize(640, 480)
            win.show()
            self.app.processEvents()
            normal_size = win.size()
            win.showMaximized()
            self.app.processEvents()
            self.assertTrue(win.isMaximized())

            win._enter_compact_mode()
            self.app.processEvents()
            win._exit_compact_mode()
            self.app.processEvents()

            self.assertFalse(win.isMaximized())
            self.assertFalse(win.isFullScreen())
            self.assertEqual(win.size(), normal_size)
            screen_center = win.screen().availableGeometry().center()
            window_center = win.frameGeometry().center()
            self.assertLessEqual(abs(window_center.x() - screen_center.x()), 3)
            self.assertLessEqual(abs(window_center.y() - screen_center.y()), 3)
        finally:
            win.close()

    def test_compact_arrows_are_disabled_while_output_is_off(self) -> None:
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
            win._enter_compact_mode()
            buttons = (
                win.compact_btn_left,
                win.compact_btn_right,
                win.compact_btn_up,
                win.compact_btn_down,
            )
            self.assertTrue(all(not button.isEnabled() for button in buttons))
            self.assertTrue(win.btn_expand.isEnabled())
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_compact_positioner_is_safe_when_not_configured(self) -> None:
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
            win._enter_compact_mode()
            win.compact_tabs.setCurrentIndex(1)
            self.app.processEvents()

            self.assertEqual(win.compact_lbl_positioner_status.text(), "Not configured")
            self.assertFalse(win.compact_btn_positioner_connect.isEnabled())
            self.assertFalse(win.compact_btn_pos_left.isEnabled())
            self.assertFalse(win.compact_btn_pos_toward.isEnabled())
            self.assertFalse(win.compact_btn_positioner_stop.isEnabled())
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_compact_positioner_buttons_emit_mapped_move(self) -> None:
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
            win._positioner_settings = ui_mod.PositionerSettings(enabled=True, port="COM4")
            win._positioner_connected = True
            win._update_positioner_controls()
            win._enter_compact_mode()
            win.compact_tabs.setCurrentIndex(1)
            win.compact_spn_positioner_steps.setValue(17)
            emitted: list[tuple[object, str, str, int]] = []
            win._positioner_move_requested.connect(
                lambda settings, axis, direction, steps: emitted.append((settings, axis, direction, steps))
            )

            win.compact_btn_pos_left.click()

            self.assertEqual(emitted[-1][1:], ("x", "left", 17))
            self.assertTrue(win._positioner_busy)
            self.assertEqual(win._daq._daq.write_calls, [])
        finally:
            win.close()

    def test_compact_arrow_key_nudges_and_escape_restores_full_view(self) -> None:
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
            win.chk_enable.setChecked(True)
            win.show()
            win._enter_compact_mode()
            win.activateWindow()
            self.app.processEvents()
            start_y = win._target_ry

            QTest.keyClick(win, Qt.Key.Key_Up)
            self.assertAlmostEqual(win._target_ry, start_y + ui_mod.STEP_PER_MOVE)

            QTest.keyClick(win, Qt.Key.Key_Escape)
            self.assertFalse(win._compact_mode)
            self.assertIs(win._view_stack.currentWidget(), win._full_page)
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
