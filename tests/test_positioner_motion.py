"""Motion lifecycle tests using the real worker and a serial protocol double."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from test_anc300_positioner import FakeSerial
from daq_xy_qt_readback import daq_xy_qt_readback as ui
from daq_xy_qt_readback.anc300_positioner import ANC300Positioner, PositionerSettings
from daq_xy_qt_readback.coordinate_transform import MappingSettings


class MotionSerial(FakeSerial):
    move_response = None
    fail_stop = False

    def write(self, payload):
        super().write(payload)
        if payload.decode().startswith("getf "):
            self._responses = [b"frequency = 100 Hz\r\n", b"OK\r\n"]
        if payload.decode().startswith(("stepu ", "stepd ")) and self.move_response:
            self._responses = list(self.move_response)
        if payload.decode().startswith("stop ") and self.fail_stop:
            self._responses = [b"ERROR: stop failed\r\n"]


class MotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.serial = MotionSerial()
        self.settings = PositionerSettings(enabled=True, port="COM4")
        self.worker = ui._PositionerWorker()
        self.worker._positioner = ANC300Positioner(serial_factory=lambda **_: self.serial)
        self.worker.connect_device(self.settings)
        self.finished = []
        self.worker.operation_finished.connect(self.finished.append)

    def tearDown(self):
        self.worker.shutdown()

    def test_finite_move_stays_busy_until_stop_confirmation(self):
        self.worker.move(self.settings, "x", "left", 100)
        self.assertEqual(self.finished, [])
        self.assertTrue(self.worker._motion_timer.isActive())
        self.assertGreaterEqual(self.worker._motion_timer.interval(), 1000)
        before = len(self.serial.writes)
        self.worker.move(self.settings, "y", "up", 1)
        self.assertEqual(len(self.serial.writes), before)
        self.worker.stop_all()
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertEqual(len(self.finished), 1)

    def test_continuous_motion_uses_c_and_shutdown_stops_before_close(self):
        self.worker.start_continuous(self.settings, "z", "away")
        self.assertEqual(self.serial.writes[-1], "stepd 6 c")
        self.assertEqual(self.finished, [])
        self.worker.shutdown()
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)

    def test_ground_cancels_pending_completion(self):
        self.worker.move(self.settings, "x", "left", 100)
        self.worker.ground_all()
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertEqual(self.finished, [])

    def test_busy_warning_stops_and_preserves_connection(self):
        self.serial.move_response = [b"WARNING: axis is already moving\r\n", b"OK\r\n"]
        self.worker.move(self.settings, "x", "left", 100)
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertTrue(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertIn("busy", self.finished[-1].lower())

    def test_unrecognized_warning_is_not_reported_as_success(self):
        self.serial.move_response = [b"WARNING: unexpected module condition\r\n", b"OK\r\n"]
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker.start_continuous(self.settings, "x", "left")
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.serial.is_open)
        self.assertEqual(self.finished, [])

    def test_elapsed_interval_confirms_stop_before_reporting_finished(self):
        self.worker.move(self.settings, "x", "left", 1)
        QTest.qWait(320)
        self.assertEqual(len(self.finished), 1)
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])

    def test_shutdown_reports_stop_failure_and_releases_serial(self):
        self.worker.start_continuous(self.settings, "x", "left")
        self.serial.fail_stop = True
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker.shutdown()
        self.assertFalse(self.serial.is_open)
        self.assertTrue(failures)

    def test_hold_release_and_deactivation_stop_without_extra_step(self):
        with mock.patch.object(ui, "load_positioner_settings", return_value=PositionerSettings()):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        try:
            win._positioner_move_requested.disconnect()
            win._positioner_continuous_requested.disconnect()
            win._positioner_stop_requested.disconnect()
            win._positioner_settings = self.settings
            win._positioner_connected = win._positioner_ready = True
            starts, stops, steps = [], [], []
            win._positioner_continuous_requested.connect(lambda *args: starts.append(args))
            win._positioner_stop_requested.connect(lambda: stops.append(True))
            win._positioner_move_requested.connect(lambda *args: steps.append(args))
            win.cmb_positioner_motion.setCurrentIndex(1)
            self.assertEqual(win.compact_cmb_positioner_motion.currentIndex(), 1)
            win.compact_tabs.setCurrentIndex(1)
            win._compact_arrow("left")
            self.assertEqual(steps, [])
            for button in (win.btn_pos_left, win.compact_btn_pos_left):
                win._update_positioner_controls()
                button.pressed.emit()
                self.assertTrue(win._positioner_busy)
                self.assertTrue(button.isEnabled())
                self.assertFalse(win.btn_pos_right.isEnabled())
                button.released.emit()
                button.clicked.emit()
                self.assertTrue(win._positioner_busy)
                win._on_positioner_operation_finished("Stopped")
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(stops), 2)
            self.assertEqual(steps, [])
            win.btn_pos_left.pressed.emit()
            QApplication.sendEvent(win, QEvent(QEvent.Type.WindowDeactivate))
            self.assertEqual(len(stops), 3)
        finally:
            win._positioner_connected = False
            win.close()

    def test_real_mouse_release_and_mode_switch_do_not_send_finite_step(self):
        with mock.patch.object(ui, "load_positioner_settings", return_value=PositionerSettings()):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        try:
            win._positioner_move_requested.disconnect()
            win._positioner_continuous_requested.disconnect()
            win._positioner_stop_requested.disconnect()
            win._positioner_settings = self.settings
            win._positioner_connected = win._positioner_ready = True
            starts, stops, steps = [], [], []
            win._positioner_continuous_requested.connect(lambda *args: starts.append(args))
            win._positioner_stop_requested.connect(lambda: stops.append(True))
            win._positioner_move_requested.connect(lambda *args: steps.append(args))
            win._enter_compact_mode()
            win.compact_tabs.setCurrentIndex(1)
            win.show()
            self.app.processEvents()
            win.compact_cmb_positioner_motion.setCurrentIndex(1)
            button = win.compact_btn_pos_left
            QTest.mousePress(button, Qt.MouseButton.LeftButton)
            self.assertEqual(len(starts), 1)
            QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
            self.assertEqual(len(stops), 1)
            self.assertTrue(win._positioner_busy)
            win._on_positioner_operation_finished("Stopped")
            QTest.mousePress(button, Qt.MouseButton.LeftButton)
            win.compact_cmb_positioner_motion.setCurrentIndex(0)
            QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
            self.assertEqual(len(stops), 2)
            self.assertEqual(steps, [])
            win._on_positioner_operation_finished("Stopped")
            win.compact_cmb_positioner_motion.setCurrentIndex(1)
            QTest.mousePress(button, Qt.MouseButton.LeftButton)
            win.compact_tabs.setCurrentIndex(0)
            self.assertEqual(len(stops), 3)
        finally:
            win._positioner_connected = False
            win.close()

    def test_window_defers_close_when_worker_has_not_finished(self):
        with mock.patch.object(ui, "load_positioner_settings", return_value=PositionerSettings()):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        try:
            event = QCloseEvent()
            with mock.patch.object(win._positioner_thread, "wait", return_value=False):
                win.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(win._positioner_close_waiting)
            self.assertFalse(win.isEnabled())
        finally:
            win._positioner_thread.wait(3000)
            win._positioner_close_waiting = False
            win.close()


if __name__ == "__main__":
    unittest.main()
