"""Persistent scanner controller that owns AO task lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from .coordinate_transform import (
    CoordinateSettings,
    ao_volts_to_physical_uv,
    coordinate_settings_from_dict,
    coordinate_settings_to_dict,
    logical_xy_to_physical_uv,
    physical_uv_to_ao_volts,
    physical_uv_to_logical_xy,
)

LOGGER = logging.getLogger(__name__)

try:
    from iv_automation import DaqControl
except Exception as exc:
    DaqControl = None
    _DAQ_IMPORT_ERROR = exc
else:
    _DAQ_IMPORT_ERROR = None


@dataclass
class ScannerState:
    """Persisted scanner runtime state."""

    logical_x: float
    logical_y: float
    ao0_v: float
    ao1_v: float

    def to_dict(self) -> dict[str, float]:
        return {
            "logical_x": self.logical_x,
            "logical_y": self.logical_y,
            "ao0_v": self.ao0_v,
            "ao1_v": self.ao1_v,
        }

    @staticmethod
    def from_dict(data: dict[str, Any], default_v: float) -> "ScannerState":
        return ScannerState(
            logical_x=float(data.get("logical_x", default_v)),
            logical_y=float(data.get("logical_y", default_v)),
            ao0_v=float(data.get("ao0_v", default_v)),
            ao1_v=float(data.get("ao1_v", default_v)),
        )


class _FakeAOBackend:
    """No-hardware backend for tests/self-checks."""

    def __init__(self, ao_x: str, ao_y: str, initial_ao0: float, initial_ao1: float) -> None:
        self.ao_x = ao_x
        self.ao_y = ao_y
        self._volts = {
            "ao0": float(initial_ao0),
            "ao1": float(initial_ao1),
            ao_x: float(initial_ao0 if ao_x == "ao0" else initial_ao1),
            ao_y: float(initial_ao0 if ao_y == "ao0" else initial_ao1),
        }

    def get_current_ao(self) -> dict[str, float]:
        return {"ao0": self._volts["ao0"], "ao1": self._volts["ao1"]}

    def set_output(self, ao0_v: float, ao1_v: float) -> None:
        self._volts["ao0"] = float(ao0_v)
        self._volts["ao1"] = float(ao1_v)
        self._volts[self.ao_x] = float(ao0_v if self.ao_x == "ao0" else ao1_v)
        self._volts[self.ao_y] = float(ao0_v if self.ao_y == "ao0" else ao1_v)

    def close(self) -> None:
        return


class _RealAOBackend:
    """Real NI-DAQ backend owning DaqControl task handles."""

    def __init__(self, dev_name: str, ao_x: str, ao_y: str) -> None:
        if DaqControl is None:
            raise RuntimeError(
                "Real DAQ control is unavailable. Ensure iv_automation.py and dependencies are installed."
            ) from _DAQ_IMPORT_ERROR
        self.ao_x = ao_x
        self.ao_y = ao_y
        self._daq = DaqControl(dev_name)
        self._vars = {ao_x: "ch0_v", ao_y: "ch1_v"}
        self._daq.add_ao_channel(ao_x, self._vars[ao_x])
        self._daq.add_ao_channel(ao_y, self._vars[ao_y])

    def get_current_ao(self) -> dict[str, float]:
        self._daq.read_y()
        x_meas = float(self._daq.send_y("measured_" + self._vars[self.ao_x]))
        y_meas = float(self._daq.send_y("measured_" + self._vars[self.ao_y]))
        if self.ao_x == "ao0":
            return {"ao0": x_meas, "ao1": y_meas}
        return {"ao0": y_meas, "ao1": x_meas}

    def set_output(self, ao0_v: float, ao1_v: float) -> None:
        by_channel = {"ao0": float(ao0_v), "ao1": float(ao1_v)}
        self._daq.receive_x(self._vars[self.ao_x], by_channel[self.ao_x])
        self._daq.receive_x(self._vars[self.ao_y], by_channel[self.ao_y])
        self._daq.write_x()

    def close(self) -> None:
        # Controller process owns tasks until explicit shutdown.
        return


class ScannerController:
    """Controller API for UI clients."""

    def __init__(
        self,
        dev_name: str,
        ao_x: str,
        ao_y: str,
        data_dir: Path,
        fake_backend: bool = False,
    ) -> None:
        self.dev_name = dev_name
        self.ao_x = ao_x
        self.ao_y = ao_y
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self.data_dir / "coord_config.json"
        self._state_path = self.data_dir / "scanner_state.json"
        self._settings = self._load_settings()
        self._state = self._load_state(default_v=self._settings.voltage_range.min_v)

        if fake_backend:
            self._backend = _FakeAOBackend(ao_x, ao_y, self._state.ao0_v, self._state.ao1_v)
        else:
            self._backend = _RealAOBackend(dev_name, ao_x, ao_y)

        self._initialize_from_hardware_or_state()

    def _load_settings(self) -> CoordinateSettings:
        default = CoordinateSettings.default_for_channels(self.ao_x, self.ao_y)
        if not self._config_path.exists():
            return default
        payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        settings = coordinate_settings_from_dict(payload, default)
        settings.validate()
        return settings

    def _save_settings(self) -> None:
        self._config_path.write_text(
            json.dumps(coordinate_settings_to_dict(self._settings), indent=2),
            encoding="utf-8",
        )

    def _load_state(self, default_v: float) -> ScannerState:
        if not self._state_path.exists():
            return ScannerState(default_v, default_v, default_v, default_v)
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ScannerState(default_v, default_v, default_v, default_v)
        return ScannerState.from_dict(payload, default_v)

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps(self._state.to_dict(), indent=2), encoding="utf-8")

    def _initialize_from_hardware_or_state(self) -> None:
        try:
            measured = self._backend.get_current_ao()
            self._state.ao0_v = float(measured["ao0"])
            self._state.ao1_v = float(measured["ao1"])
            u, v = ao_volts_to_physical_uv(measured, self._settings)
            x, y = physical_uv_to_logical_xy(u, v, self._settings)
            self._state.logical_x = x
            self._state.logical_y = y
            self._save_state()
            LOGGER.info("Controller initialized from hardware readback.")
            return
        except Exception:
            LOGGER.warning("Hardware readback unavailable at startup; restoring from persisted state.")

        # Fallback when readback is unavailable: write saved output once, then hold.
        self._backend.set_output(self._state.ao0_v, self._state.ao1_v)
        self._save_state()

    def get_state(self) -> dict[str, float]:
        return self._state.to_dict()

    def get_config(self) -> dict[str, Any]:
        return coordinate_settings_to_dict(self._settings)

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        new_settings = coordinate_settings_from_dict(
            config,
            CoordinateSettings.default_for_channels(self.ao_x, self.ao_y),
        )
        mapped = {new_settings.axis_map.u, new_settings.axis_map.v}
        if mapped != {self.ao_x, self.ao_y}:
            raise ValueError(
                f"Configured axis_map must reference exactly {sorted({self.ao_x, self.ao_y})}"
            )
        self._settings = new_settings
        self._save_settings()
        return self.get_config()

    def set_output(self, ao0_v: float, ao1_v: float) -> dict[str, float]:
        self._backend.set_output(ao0_v, ao1_v)
        self._state.ao0_v = float(ao0_v)
        self._state.ao1_v = float(ao1_v)
        u, v = ao_volts_to_physical_uv({"ao0": ao0_v, "ao1": ao1_v}, self._settings)
        x, y = physical_uv_to_logical_xy(u, v, self._settings)
        self._state.logical_x = x
        self._state.logical_y = y
        self._save_state()
        return self.get_state()

    def move_logical(self, x: float, y: float) -> dict[str, float]:
        u, v = logical_xy_to_physical_uv(float(x), float(y), self._settings)
        ao_volts, _ = physical_uv_to_ao_volts(u, v, self._settings)
        return self.set_output(float(ao_volts["ao0"]), float(ao_volts["ao1"]))

    def jog_logical(self, dx: float, dy: float) -> dict[str, float]:
        return self.move_logical(self._state.logical_x + float(dx), self._state.logical_y + float(dy))

