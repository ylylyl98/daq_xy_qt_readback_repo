"""Tests for optional ANC300 mapping and serial command safety."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from daq_xy_qt_readback.anc300_positioner import (
    ANC300Positioner,
    PositionerSettings,
    load_positioner_settings,
    save_positioner_settings,
)


class FakeSerial:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.is_open = True
        self.writes: list[str] = []
        self._responses: list[bytes] = []
        self.modes: dict[str, str] = {}

    def reset_input_buffer(self) -> None:
        pass

    def write(self, payload: bytes) -> None:
        command = payload.decode("ascii").strip()
        self.writes.append(command)
        if command == "ver":
            body = "attocube ANC300 test firmware"
        elif command.startswith("getm "):
            axis = command.split()[1]
            body = self.modes.get(axis, "stp")
        elif command.startswith("setm "):
            _, axis, mode = command.split()
            self.modes[axis] = mode
            body = "OK"
        else:
            body = "OK"
        self._responses = [f"{command}\r\n".encode(), f"{body}\r\n".encode()]
        if body != "OK":
            self._responses.append(b"OK\r\n")

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return self._responses.pop(0) if self._responses else b""

    def close(self) -> None:
        self.is_open = False


class StuckOpenSerial(FakeSerial):
    def close(self) -> None:
        pass


class FailingCloseSerial(FakeSerial):
    def close(self) -> None:
        raise OSError("port close failed")


class PositionerMappingTests(unittest.TestCase):
    def test_physical_directions_map_through_user_choices(self) -> None:
        settings = PositionerSettings(
            enabled=True,
            port="COM9",
            x_axis=4,
            y_axis=5,
            z_axis=6,
            x_positive="right",
            y_positive="down",
            z_positive="away",
        )
        self.assertFalse(settings.is_positive_move("x", "left"))
        self.assertTrue(settings.is_positive_move("x", "right"))
        self.assertFalse(settings.is_positive_move("y", "up"))
        self.assertFalse(settings.is_positive_move("z", "toward"))

    def test_duplicate_axes_are_rejected(self) -> None:
        settings = PositionerSettings(enabled=True, port="COM4", x_axis=4, y_axis=4, z_axis=6)
        with self.assertRaisesRegex(ValueError, "different"):
            settings.validate()

    def test_scanner_and_positioner_axes_cannot_overlap(self) -> None:
        settings = PositionerSettings(
            enabled=True,
            port="COM4",
            scanner_x_axis=1,
            scanner_y_axis=4,
            x_axis=4,
            y_axis=5,
            z_axis=6,
        )
        with self.assertRaisesRegex(ValueError, "different ANC300 axes"):
            settings.validate()

    def test_zero_tolerance_has_safe_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero tolerance"):
            PositionerSettings(scanner_zero_tolerance_v=0.0001).validate()

    def test_settings_round_trip(self) -> None:
        settings = PositionerSettings(enabled=True, port="COM7", x_axis=4, y_axis=5, z_axis=7)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "positioner_settings.json"
            save_positioner_settings(path, settings)
            self.assertEqual(load_positioner_settings(path), settings)


class ANC300ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.serial = FakeSerial()
        self.positioner = ANC300Positioner(serial_factory=lambda **_: self.serial)
        self.settings = PositionerSettings(enabled=True, port="COM4", x_axis=4, y_axis=5, z_axis=6)

    def test_connect_only_identifies_and_queries_configured_axes(self) -> None:
        self.positioner.connect(self.settings)
        self.assertEqual(self.serial.writes, ["ver", "getm 4", "getm 5", "getm 6"])
        self.assertFalse(any(command.startswith(("stepu", "stepd", "stop")) for command in self.serial.writes))

    def test_moves_use_saved_axes_and_direction_correspondence(self) -> None:
        self.positioner.connect(self.settings)
        self.positioner.move(self.settings, "x", "left", 10)
        self.positioner.move(self.settings, "x", "right", 11)
        self.positioner.move(self.settings, "z", "away", 5)
        self.assertEqual(
            self.serial.writes[-6:],
            ["getm 4", "stepu 4 10", "getm 4", "stepd 4 11", "getm 6", "stepd 6 5"],
        )
        self.assertFalse(any(" 1 " in command or " 2 " in command or " 3 " in command for command in self.serial.writes))

    def test_ground_all_uses_anc300_ground_mode(self) -> None:
        self.positioner.connect(self.settings)
        detail = self.positioner.ground_all()
        self.assertEqual(
            self.serial.writes[-9:],
            [
                "stop 4", "setm 4 gnd", "getm 4",
                "stop 5", "setm 5 gnd", "getm 5",
                "stop 6", "setm 6 gnd", "getm 6",
            ],
        )
        self.assertEqual(detail, "Grounded ANC300 axes: 4, 5, 6")

    def test_grounded_positioner_cannot_move_until_explicitly_enabled(self) -> None:
        self.positioner.connect(self.settings)
        self.positioner.ground_all()
        before = list(self.serial.writes)
        with self.assertRaisesRegex(RuntimeError, "not enabled for stepping"):
            self.positioner.move(self.settings, "x", "left", 10)
        self.assertEqual(self.serial.writes[len(before):], ["getm 4"])

    def test_enable_all_uses_anc300_stepping_mode(self) -> None:
        self.positioner.connect(self.settings)
        self.positioner.ground_all()
        detail = self.positioner.enable_all()
        self.assertEqual(
            self.serial.writes[-6:],
            ["setm 4 stp", "getm 4", "setm 5 stp", "getm 5", "setm 6 stp", "getm 6"],
        )
        self.assertEqual(detail, "Enabled ANC300 stepping on axes: 4, 5, 6")

    def test_scanner_mode_commands_only_use_scanner_axes(self) -> None:
        self.positioner.connect(self.settings)
        ground_detail = self.positioner.ground_scanner(self.settings)
        self.assertEqual(
            self.serial.writes[-4:],
            ["setm 1 gnd", "getm 1", "setm 2 gnd", "getm 2"],
        )
        enable_detail = self.positioner.enable_scanner(self.settings)
        self.assertEqual(
            self.serial.writes[-4:],
            ["setm 1 stp", "getm 1", "setm 2 stp", "getm 2"],
        )
        self.assertEqual(ground_detail, "Grounded ANC300 scanner axes: 1, 2")
        self.assertEqual(enable_detail, "Enabled ANC300 scanner axes: 1, 2")

    def test_z_single_move_limit_is_enforced(self) -> None:
        self.positioner.connect(self.settings)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            self.positioner.move(self.settings, "z", "toward", 101)

    def test_close_sends_no_command(self) -> None:
        self.positioner.connect(self.settings)
        before = list(self.serial.writes)
        self.positioner.close()
        self.assertEqual(self.serial.writes, before)

    def test_close_rejects_a_port_that_remains_open(self) -> None:
        serial_port = StuckOpenSerial()
        positioner = ANC300Positioner(serial_factory=lambda **_: serial_port)
        positioner.connect(self.settings)

        with self.assertRaisesRegex(RuntimeError, "remained open"):
            positioner.close()
        self.assertTrue(positioner.connected)

    def test_close_reports_driver_failure_and_keeps_open_handle(self) -> None:
        serial_port = FailingCloseSerial()
        positioner = ANC300Positioner(serial_factory=lambda **_: serial_port)
        positioner.connect(self.settings)

        with self.assertRaisesRegex(RuntimeError, "Unable to release"):
            positioner.close()
        self.assertTrue(positioner.connected)


if __name__ == "__main__":
    unittest.main()
