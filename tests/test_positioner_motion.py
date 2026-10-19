"""Motion lifecycle tests using the real worker and a serial protocol double."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from dataclasses import replace
from threading import Event
from unittest import mock
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QTabWidget
from test_anc300_positioner import FakeSerial
from daq_xy_qt_readback import daq_xy_qt_readback as ui
from daq_xy_qt_readback.anc300_positioner import ANC300Positioner, PositionerSettings
from daq_xy_qt_readback.coordinate_transform import MappingSettings


class MotionSerial(FakeSerial):
    move_response = None
    wait_response = None
    fail_stop = False
    frequency_response = (b"frequency = 100 Hz\r\n", b"OK\r\n")

    def write(self, payload):
        super().write(payload)
        if payload.decode().startswith("getf "):
            self._responses = list(self.frequency_response)
        if payload.decode().startswith(("stepu ", "stepd ")) and self.move_response:
            self._responses = list(self.move_response)
        if payload.decode().startswith("stepw ") and self.wait_response is not None:
            self._responses = list(self.wait_response)
        if payload.decode().startswith("stop ") and self.fail_stop:
            self._responses = [b"ERROR: stop failed\r\n"]


class GatedStepSerial(MotionSerial):
    """Hold the controller's completion reply until the test releases it."""

    def __init__(self):
        super().__init__()
        self.step_waiting = Event()
        self.step_complete = Event()
        self._waiting = False

    def write(self, payload):
        super().write(payload)
        self._waiting = payload.decode().startswith("stepw ")
        if self._waiting:
            self.step_waiting.set()

    def readline(self):
        if self._waiting and not self.step_complete.wait(0.05):
            return b""
        return super().readline()


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

    def test_prompted_frequency_echo_allows_up_and_down_with_correct_duration(self):
        self.serial.frequency_response = (
            b"> getf 5\r\n", b"frequency = 60 Hz\r\n", b"OK\r\n",
        )
        failures = []
        self.worker.failed.connect(failures.append)
        for direction, command in (("up", "stepu 5 60"), ("down", "stepd 5 60")):
            before = len(self.serial.writes)
            self.worker.move(self.settings, "y", direction, 60)
            self.assertEqual(failures, [])
            self.assertEqual(self.serial.writes[before:], ["getf 5", "getm 5", command])
            # Sixty steps at 60 Hz take one second, plus the 250 ms guard.
            self.assertGreaterEqual(self.worker._motion_timer.interval(), 1250)
            self.assertLessEqual(self.worker._motion_timer.interval(), 1251)
            self.assertTrue(self.worker._motion_timer.isActive())
            self.worker.stop_all()

    def test_unexpected_frequency_data_never_sends_a_move(self):
        self.serial.frequency_response = (
            b"> getf 5\r\n", b"frequency = 60 Hz\r\n", b"unexpected data\r\n", b"OK\r\n",
        )
        failures = []
        self.worker.failed.connect(failures.append)
        before = len(self.serial.writes)
        self.worker.move(self.settings, "y", "up", 60)
        self.assertEqual(len(failures), 1)
        self.assertIn("unexpected data", failures[0])
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertEqual(self.serial.writes[before:], ["getf 5", "stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)

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

    def test_continuous_motion_repeats_single_steps_until_stopped(self):
        self.worker.start_continuous(self.settings, "y", "up")
        self.assertEqual(self.serial.writes[-2:], ["stepu 5 1", "stepw 5"])
        self.assertTrue(self.worker._motion_timer.isActive())
        self.assertEqual(self.finished, [])
        QTest.qWait(320)
        moves = [command for command in self.serial.writes if command.startswith(("stepu", "stepd"))]
        self.assertGreaterEqual(len(moves), 2)
        self.assertTrue(all(command == "stepu 5 1" for command in moves))
        self.assertEqual(self.serial.writes.count("stepw 5"), len(moves))
        self.assertNotIn("getf 5", self.serial.writes)
        self.assertEqual(self.finished, [])

        self.worker.stop_all()
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        QTest.qWait(320)
        self.assertEqual(self.serial.writes, before)

    def test_continuous_shutdown_stops_before_close_and_cancels_repeats(self):
        self.worker.start_continuous(self.settings, "z", "away")
        self.assertEqual(self.serial.writes[-2:], ["stepd 6 1", "stepw 6"])
        self.assertEqual(self.finished, [])
        self.worker.shutdown()
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes, before)

    def test_continuous_ground_cancels_repeats(self):
        self.worker.start_continuous(self.settings, "x", "left")
        self.worker.ground_all()
        self.assertFalse(self.worker._motion_timer.isActive())
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes, before)

    def test_continuous_disconnect_cancels_repeats_even_if_stop_fails(self):
        self.worker.start_continuous(self.settings, "x", "left")
        self.serial.fail_stop = True
        failures = []
        self.worker.disconnect_failed.connect(failures.append)
        self.worker.disconnect_device()
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.worker._motion_timer.isActive())
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes, before)

    def test_failed_finite_disconnect_retries_stop_before_closing_port(self):
        self.serial.frequency_response = (b"frequency = 1 Hz\r\n", b"OK\r\n")
        self.worker.move(self.settings, "x", "left", 1000)
        self.serial.fail_stop = True
        disconnected = []
        self.worker.disconnected.connect(disconnected.append)
        self.worker.disconnect_device()
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertTrue(self.serial.is_open)
        self.assertEqual(disconnected, [])

        self.serial.fail_stop = False
        before = len(self.serial.writes)
        self.worker.disconnect_device()
        self.assertEqual(self.serial.writes[before:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)
        self.assertEqual(len(disconnected), 1)

    def test_shutdown_retries_stop_after_failed_finite_disconnect(self):
        self.worker.move(self.settings, "x", "left", 1000)
        self.serial.fail_stop = True
        self.worker.disconnect_device()

        self.serial.fail_stop = False
        before = len(self.serial.writes)
        self.worker.shutdown()
        self.assertEqual(self.serial.writes[before:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)

    def test_new_motion_and_enable_require_stop_after_failed_disconnect(self):
        for operation in (
            lambda: self.worker.move(self.settings, "y", "up", 1),
            lambda: self.worker.start_continuous(self.settings, "y", "up"),
            self.worker.enable_all,
        ):
            with self.subTest(operation=operation):
                self.worker.move(self.settings, "x", "left", 1000)
                self.serial.fail_stop = True
                self.worker.disconnect_device()
                self.serial.fail_stop = False
                before = len(self.serial.writes)
                self.finished.clear()
                operation()
                self.assertEqual(self.serial.writes[before:], ["stop 4", "stop 5", "stop 6"])
                self.assertTrue(self.serial.is_open)
                self.assertFalse(self.worker._motion_timer.isActive())
                self.assertIn("Retry", self.finished[-1])

    def test_reconnect_confirms_previous_stop_or_closes_failed_connection(self):
        for stop_fails in (False, True):
            with self.subTest(stop_fails=stop_fails):
                first_serial, retry_serial = MotionSerial(), MotionSerial()
                serials = iter((first_serial, retry_serial))
                worker = ui._PositionerWorker()
                worker._positioner = ANC300Positioner(serial_factory=lambda **_: next(serials))
                connected, failures = [], []
                worker.connected.connect(connected.append)
                worker.failed.connect(failures.append)
                try:
                    worker.connect_device(self.settings)
                    worker.move(self.settings, "x", "left", 1000)
                    first_serial.fail_stop = True
                    worker.disconnect_device()
                    # A new movement cannot overwrite the unconfirmed STOP, even
                    # when retrying STOP also fails and closes the connection.
                    before = len(first_serial.writes)
                    worker.move(self.settings, "y", "up", 1)
                    self.assertTrue(all(command.startswith("stop ") for command in first_serial.writes[before:]))
                    self.assertFalse(first_serial.is_open)
                    connected.clear()
                    failures.clear()

                    retry_serial.fail_stop = stop_fails
                    worker.connect_device(self.settings)
                    self.assertEqual(retry_serial.writes[4:7], ["stop 4", "stop 5", "stop 6"])
                    if stop_fails:
                        self.assertEqual(connected, [])
                        self.assertTrue(failures)
                        self.assertFalse(retry_serial.is_open)
                    else:
                        self.assertEqual(len(connected), 1)
                        self.assertEqual(failures, [])
                        self.assertTrue(retry_serial.is_open)
                finally:
                    worker.shutdown()

    def test_unconfirmed_stop_rejects_reconnect_to_different_port_or_axes(self):
        for changed in (
            replace(self.settings, port="COM9"),
            replace(self.settings, x_axis=7),
            replace(self.settings, baudrate=9600),
        ):
            with self.subTest(settings=changed):
                first_serial, retry_serial = MotionSerial(), MotionSerial()
                factory = mock.Mock(side_effect=(first_serial, retry_serial))
                worker = ui._PositionerWorker()
                worker._positioner = ANC300Positioner(serial_factory=factory)
                connected, failures = [], []
                worker.connected.connect(connected.append)
                worker.failed.connect(failures.append)
                try:
                    worker.connect_device(self.settings)
                    worker.move(self.settings, "x", "left", 1000)
                    first_serial.fail_stop = True
                    worker.stop_all()
                    self.assertFalse(first_serial.is_open)
                    connected.clear()
                    failures.clear()

                    worker.connect_device(changed)
                    self.assertEqual(factory.call_count, 1)
                    self.assertEqual(retry_serial.writes, [])
                    self.assertEqual(connected, [])
                    self.assertIn("COM4, axes 4, 5, 6", failures[-1])

                    worker.connect_device(self.settings)
                    self.assertEqual(factory.call_count, 2)
                    self.assertEqual(retry_serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
                    self.assertEqual(len(connected), 1)
                finally:
                    worker.shutdown()

    def test_reconfigure_open_connection_confirms_stop_on_original_axes_first(self):
        for stop_fails in (False, True):
            with self.subTest(stop_fails=stop_fails):
                first_serial, next_serial = MotionSerial(), MotionSerial()
                factory = mock.Mock(side_effect=(first_serial, next_serial))
                worker = ui._PositionerWorker()
                worker._positioner = ANC300Positioner(serial_factory=factory)
                connected = []
                worker.connected.connect(connected.append)
                try:
                    worker.connect_device(self.settings)
                    worker.move(self.settings, "x", "left", 1000)
                    first_serial.fail_stop = True
                    worker.disconnect_device()
                    self.assertTrue(first_serial.is_open)
                    connected.clear()

                    first_serial.fail_stop = stop_fails
                    before = len(first_serial.writes)
                    worker.connect_device(replace(self.settings, port="COM9", x_axis=7))
                    self.assertEqual(first_serial.writes[before:before + 3], ["stop 4", "stop 5", "stop 6"])
                    if stop_fails:
                        self.assertEqual(factory.call_count, 1)
                        self.assertEqual(next_serial.writes, [])
                        self.assertEqual(connected, [])
                    else:
                        self.assertEqual(factory.call_count, 2)
                        self.assertEqual(next_serial.writes, ["ver", "getm 7", "getm 5", "getm 6"])
                        self.assertEqual(len(connected), 1)
                finally:
                    worker.shutdown()

    def test_continuous_repeat_error_stops_loop_and_closes_connection(self):
        self.worker.start_continuous(self.settings, "x", "right")
        self.serial.move_response = [b"ERROR: step failed\r\n"]
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(len(failures), 1)
        self.assertIn("step failed", failures[0])
        self.assertFalse(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes, before)

    def test_continuous_repeat_busy_stops_loop_and_keeps_connection(self):
        self.worker.start_continuous(self.settings, "x", "left")
        self.serial.move_response = [b"WARNING: axis is already moving\r\n", b"OK\r\n"]
        self.worker._motion_timer.timeout.emit()
        self.assertIn("busy", self.finished[-1].lower())
        self.assertTrue(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])

    def test_finite_move_after_continuous_uses_selected_count_without_repeating(self):
        self.worker.start_continuous(self.settings, "x", "left")
        self.worker.stop_all()
        self.worker.move(self.settings, "y", "down", 17)
        self.assertEqual(self.serial.writes[-1], "stepd 5 17")
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.worker._motion_timer.isActive())

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
        self.worker.move(self.settings, "x", "left", 2)
        QTest.qWait(320)
        self.assertEqual(len(self.finished), 1)
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])

    def test_single_step_completes_on_controller_ack_without_guard_or_stop(self):
        for axis, direction, command, number in (
            ("x", "left", "stepu", 4), ("x", "right", "stepd", 4),
            ("y", "up", "stepu", 5), ("y", "down", "stepd", 5),
            ("z", "toward", "stepu", 6), ("z", "away", "stepd", 6),
        ):
            with self.subTest(axis=axis, direction=direction):
                before = len(self.serial.writes)
                self.finished.clear()
                self.worker.move(self.settings, axis, direction, 1)
                self.assertEqual(self.serial.writes[before:], [
                    f"getm {number}", f"{command} {number} 1", f"stepw {number}",
                ])
                self.assertEqual(len(self.finished), 1)
                self.assertFalse(self.worker._motion_active)
                self.assertFalse(self.worker._motion_timer.isActive())

    def test_failed_single_step_completion_stops_and_disconnects(self):
        self.serial.wait_response = [b"ERROR: wait failed\r\n"]
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker.move(self.settings, "x", "left", 1)
        self.assertEqual(self.finished, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("wait failed", failures[0])
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())

    def test_missing_single_step_completion_never_reports_success(self):
        self.serial.wait_response = []
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker.move(self.settings, "x", "left", 1)
        self.assertEqual(self.finished, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("No complete response", failures[0])
        self.assertIn("stepw 4", failures[0])
        self.assertEqual(self.serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
        self.assertFalse(self.serial.is_open)

    def test_continuous_completion_failure_cancels_repeats(self):
        self.serial.wait_response = [b"ERROR: wait failed\r\n"]
        failures = []
        self.worker.failed.connect(failures.append)
        self.worker.start_continuous(self.settings, "y", "down")
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.finished, [])
        self.assertFalse(self.serial.is_open)
        self.assertFalse(self.worker._motion_timer.isActive())
        before = list(self.serial.writes)
        self.worker._motion_timer.timeout.emit()
        self.assertEqual(self.serial.writes, before)

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

    def test_threaded_hold_repeats_one_step_and_ui_actions_cancel_repeats(self):
        serial = MotionSerial()
        with mock.patch.object(ui, "load_positioner_settings", return_value=self.settings):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        win._positioner_worker._positioner = ANC300Positioner(serial_factory=lambda **_: serial)

        def wait_for(predicate):
            for _ in range(200):
                if predicate():
                    return
                QTest.qWait(10)
            self.fail("Positioner worker did not reach the expected state")

        try:
            win.show()
            win._enter_compact_mode()
            win.compact_tabs.setCurrentIndex(1)
            win.compact_btn_positioner_connect.click()
            wait_for(lambda: win._positioner_connected and not win._positioner_busy)
            win.compact_btn_positioner_enable.click()
            wait_for(lambda: win._positioner_ready and not win._positioner_busy)
            win.compact_spn_positioner_steps.setValue(37)
            button = win.compact_btn_pos_left
            for action in ("release", "mode", "tab", "deactivate"):
                with self.subTest(action=action):
                    win.compact_tabs.setCurrentIndex(1)
                    win.compact_cmb_positioner_motion.setCurrentIndex(1)
                    before = len(serial.writes)
                    QTest.mousePress(button, Qt.MouseButton.LeftButton)
                    wait_for(lambda: serial.writes[before:].count("stepu 4 1") >= 2)
                    self.assertTrue(win._positioner_busy)
                    if action == "mode":
                        win.compact_cmb_positioner_motion.setCurrentIndex(0)
                    elif action == "tab":
                        win.compact_tabs.setCurrentIndex(0)
                    elif action == "deactivate":
                        QApplication.sendEvent(win, QEvent(QEvent.Type.WindowDeactivate))
                    QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
                    wait_for(lambda: not win._positioner_busy)
                    self.assertEqual(serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
                    stopped_writes = list(serial.writes)
                    QTest.qWait(320)
                    self.assertEqual(serial.writes, stopped_writes)
            moves = [command for command in serial.writes if command.startswith(("stepu", "stepd"))]
            self.assertTrue(all(command == "stepu 4 1" for command in moves))
            self.assertEqual(win.spn_positioner_steps.value(), 37)
            self.assertIsNone(win._daq)
        finally:
            win.close()

    def test_setup_disconnect_failure_retries_stop_for_finite_move(self):
        serial = MotionSerial()
        serial.frequency_response = (b"frequency = 1 Hz\r\n", b"OK\r\n")
        with mock.patch.object(ui, "load_positioner_settings", return_value=self.settings):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        win._positioner_worker._positioner = ANC300Positioner(serial_factory=lambda **_: serial)

        def wait_for(predicate):
            for _ in range(200):
                if predicate():
                    return
                QTest.qWait(10)
            self.fail("Positioner worker did not reach the expected state")

        try:
            win.show()
            tabs = win.findChild(QTabWidget, "controlTabs")
            tabs.setCurrentIndex(1)
            win.btn_positioner_connect.click()
            wait_for(lambda: win._positioner_connected and not win._positioner_busy)
            win.btn_positioner_enable.click()
            wait_for(lambda: win._positioner_ready and not win._positioner_busy)
            win.spn_positioner_steps.setValue(1000)
            win.btn_pos_left.click()
            wait_for(lambda: "stepu 4 1000" in serial.writes)
            self.assertTrue(win._positioner_busy)

            tabs.setCurrentIndex(2)
            win.cmb_pos_x_positive.setCurrentIndex(win.cmb_pos_x_positive.findData("right"))
            self.assertTrue(win.btn_positioner_apply.isEnabled())
            failures = []
            win._positioner_worker.disconnect_failed.connect(failures.append)
            serial.fail_stop = True
            with mock.patch.object(ui, "save_positioner_settings"):
                win.btn_positioner_apply.click()
            wait_for(lambda: failures and not win._positioner_busy)
            self.assertTrue(win._positioner_connected)
            self.assertTrue(serial.is_open)

            serial.fail_stop = False
            before = len(serial.writes)
            tabs.setCurrentIndex(1)
            win.btn_positioner_connect.click()
            wait_for(lambda: not win._positioner_connected and not win._positioner_busy)
            self.assertEqual(serial.writes[before:], ["stop 4", "stop 5", "stop 6"])
            self.assertFalse(serial.is_open)
        finally:
            serial.fail_stop = False
            win.close()

    def test_threaded_single_step_ack_unlocks_clicks_and_release_cancels_held_move(self):
        serial = GatedStepSerial()
        with mock.patch.object(ui, "load_positioner_settings", return_value=self.settings):
            win = ui.DaqXYWindow("Dev1", "ao0", "ao1", MappingSettings(), [], {})
        win._positioner_worker._positioner = ANC300Positioner(serial_factory=lambda **_: serial)

        def wait_for(predicate):
            for _ in range(200):
                if predicate():
                    return
                QTest.qWait(10)
            self.fail("Positioner worker did not reach the expected state")

        try:
            win.show()
            win._enter_compact_mode()
            win.compact_tabs.setCurrentIndex(1)
            win.compact_btn_positioner_connect.click()
            wait_for(lambda: win._positioner_connected and not win._positioner_busy)
            win.compact_btn_positioner_enable.click()
            wait_for(lambda: win._positioner_ready and not win._positioner_busy)
            win.compact_spn_positioner_steps.setValue(1)
            for button, command in (
                (win.compact_btn_pos_left, "stepu 4 1"),
                (win.compact_btn_pos_right, "stepd 4 1"),
            ):
                serial.step_waiting.clear()
                serial.step_complete.clear()
                before = len(serial.writes)
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                wait_for(serial.step_waiting.is_set)
                QTest.qWait(20)
                self.assertTrue(win._positioner_busy)
                self.assertFalse(button.isEnabled())
                button.click()
                self.assertEqual(serial.writes[before:], ["getm 4", command, "stepw 4"])

                serial.step_complete.set()
                wait_for(lambda: not win._positioner_busy)
                self.assertTrue(button.isEnabled())
                self.assertTrue(win.btn_pos_left.isEnabled())
                self.assertEqual(serial.writes[before:], ["getm 4", command, "stepw 4"])

            # Releasing during stepw must service STOP before issuing another step.
            win.compact_cmb_positioner_motion.setCurrentIndex(1)
            serial.step_waiting.clear()
            serial.step_complete.clear()
            before = len(serial.writes)
            QTest.mousePress(win.compact_btn_pos_left, Qt.MouseButton.LeftButton)
            wait_for(serial.step_waiting.is_set)
            QTest.qWait(20)
            self.assertEqual(serial.writes[before:], ["getm 4", "stepu 4 1", "stepw 4"])
            QTest.mouseRelease(win.compact_btn_pos_left, Qt.MouseButton.LeftButton)
            serial.step_complete.set()
            wait_for(lambda: not win._positioner_busy)
            self.assertEqual(serial.writes[before:], [
                "getm 4", "stepu 4 1", "stepw 4", "stop 4", "stop 5", "stop 6",
            ])
            QTest.qWait(50)
            self.assertEqual(serial.writes[-3:], ["stop 4", "stop 5", "stop 6"])
            self.assertIsNone(win._daq)
        finally:
            serial.step_complete.set()
            win.close()


if __name__ == "__main__":
    unittest.main()
