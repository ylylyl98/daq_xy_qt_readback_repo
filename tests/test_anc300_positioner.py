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

    def reset_input_buffer(self) -> None:
        pass

    def write(self, payload: bytes) -> None:
        command = payload.decode("ascii").strip()
        self.writes.append(command)
        if command == "ver":
            body = "attocube ANC300 test firmware"
        elif command.startswith("getm "):
            body = "stp"
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

    def test_settings_round_trip(self) -> None:
        settings = PositionerSettings(enabled=True, port="COM7", x_axis=2, y_axis=3, z_axis=7)
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
        self.assertEqual(self.serial.writes[-3:], ["stepu 4 10", "stepd 4 11", "stepd 6 5"])
        self.assertFalse(any(" 1 " in command or " 2 " in command or " 3 " in command for command in self.serial.writes))

    def test_z_single_move_limit_is_enforced(self) -> None:
        self.positioner.connect(self.settings)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            self.positioner.move(self.settings, "z", "toward", 101)

    def test_close_sends_no_command(self) -> None:
        self.positioner.connect(self.settings)
        before = list(self.serial.writes)
        self.positioner.close()
        self.assertEqual(self.serial.writes, before)


if __name__ == "__main__":
    unittest.main()
