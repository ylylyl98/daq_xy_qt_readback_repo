"""Optional ANC300 serial backend and per-PC positioner mapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable


VALID_X_DIRECTIONS = ("left", "right")
VALID_Y_DIRECTIONS = ("up", "down")
VALID_Z_DIRECTIONS = ("toward", "away")


@dataclass(frozen=True)
class PositionerSettings:
    """Machine-local mapping between physical directions and ANC300 axes."""

    enabled: bool = False
    port: str = ""
    baudrate: int = 38400
    x_axis: int = 4
    y_axis: int = 5
    z_axis: int = 6
    x_positive: str = "left"
    y_positive: str = "up"
    z_positive: str = "toward"

    def validate(self) -> None:
        axes = (self.x_axis, self.y_axis, self.z_axis)
        if any(axis < 1 or axis > 7 for axis in axes):
            raise ValueError("Positioner axes must be between 1 and 7.")
        if len(set(axes)) != 3:
            raise ValueError("X, Y, and Z must use different ANC300 axes.")
        if self.enabled and not self.port.strip():
            raise ValueError("Select a COM port before enabling the positioner.")
        if self.baudrate <= 0:
            raise ValueError("The serial baud rate must be positive.")
        if self.x_positive not in VALID_X_DIRECTIONS:
            raise ValueError("Invalid X positive-direction mapping.")
        if self.y_positive not in VALID_Y_DIRECTIONS:
            raise ValueError("Invalid Y positive-direction mapping.")
        if self.z_positive not in VALID_Z_DIRECTIONS:
            raise ValueError("Invalid Z positive-direction mapping.")

    def axis_number(self, axis: str) -> int:
        try:
            return {"x": self.x_axis, "y": self.y_axis, "z": self.z_axis}[axis]
        except KeyError as exc:
            raise ValueError(f"Unknown positioner axis: {axis}") from exc

    def positive_direction(self, axis: str) -> str:
        try:
            return {"x": self.x_positive, "y": self.y_positive, "z": self.z_positive}[axis]
        except KeyError as exc:
            raise ValueError(f"Unknown positioner axis: {axis}") from exc

    def is_positive_move(self, axis: str, physical_direction: str) -> bool:
        allowed = {
            "x": set(VALID_X_DIRECTIONS),
            "y": set(VALID_Y_DIRECTIONS),
            "z": set(VALID_Z_DIRECTIONS),
        }
        if axis not in allowed or physical_direction not in allowed[axis]:
            raise ValueError(f"Invalid {axis.upper()} direction: {physical_direction}")
        return physical_direction == self.positive_direction(axis)


def load_positioner_settings(path: Path) -> PositionerSettings:
    if not path.exists():
        return PositionerSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return PositionerSettings()
        settings = PositionerSettings(
            enabled=bool(payload.get("enabled", False)),
            port=str(payload.get("port", "")),
            baudrate=int(payload.get("baudrate", 38400)),
            x_axis=int(payload.get("x_axis", 4)),
            y_axis=int(payload.get("y_axis", 5)),
            z_axis=int(payload.get("z_axis", 6)),
            x_positive=str(payload.get("x_positive", "left")).lower(),
            y_positive=str(payload.get("y_positive", "up")).lower(),
            z_positive=str(payload.get("z_positive", "toward")).lower(),
        )
        settings.validate()
        return settings
    except Exception:
        return PositionerSettings()


def save_positioner_settings(path: Path, settings: PositionerSettings) -> None:
    settings.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def list_serial_ports() -> list[str]:
    """Return available serial device names without opening any device."""
    try:
        from serial.tools import list_ports

        return sorted(str(port.device) for port in list_ports.comports())
    except Exception:
        return []


class ANC300Positioner:
    """Small synchronous ANC300 protocol wrapper; call it from a worker thread."""

    def __init__(self, serial_factory: Callable[..., Any] | None = None) -> None:
        self._serial_factory = serial_factory
        self._serial: Any | None = None
        self._axes: tuple[int, int, int] = ()

    @property
    def connected(self) -> bool:
        return bool(self._serial is not None and getattr(self._serial, "is_open", True))

    def connect(self, settings: PositionerSettings) -> str:
        settings.validate()
        if not settings.enabled:
            raise RuntimeError("Positioner support is disabled in Setup.")
        self.close()
        factory = self._serial_factory
        if factory is None:
            try:
                import serial
            except Exception as exc:
                raise RuntimeError("pyserial is required for ANC300 support.") from exc
            factory = serial.Serial
        try:
            self._serial = factory(
                port=settings.port,
                baudrate=settings.baudrate,
                timeout=0.75,
                write_timeout=0.75,
            )
            reset = getattr(self._serial, "reset_input_buffer", None)
            if callable(reset):
                reset()
            version = self._exchange("ver")
            normalized = version.lower()
            if "anc300" not in normalized or "attocube" not in normalized:
                raise RuntimeError(f"Unexpected device response on {settings.port}: {version or '<empty>'}")
            axes = (settings.x_axis, settings.y_axis, settings.z_axis)
            for axis in axes:
                mode = self._exchange(f"getm {axis}")
                if "stp" not in mode.lower():
                    raise RuntimeError(f"ANC300 axis {axis} is not in stepping mode: {mode or '<empty>'}")
            self._axes = axes
            return version
        except Exception:
            self.close()
            raise

    def move(self, settings: PositionerSettings, axis: str, physical_direction: str, steps: int) -> str:
        if not self.connected:
            raise RuntimeError("ANC300 is not connected.")
        steps = int(steps)
        maximum = 100 if axis == "z" else 1000
        if steps < 1 or steps > maximum:
            raise ValueError(f"{axis.upper()} move must be between 1 and {maximum} steps.")
        axis_number = settings.axis_number(axis)
        if axis_number not in self._axes:
            raise RuntimeError("The saved axis mapping does not match the active ANC300 connection.")
        command = "stepu" if settings.is_positive_move(axis, physical_direction) else "stepd"
        return self._exchange(f"{command} {axis_number} {steps}")

    def stop_all(self) -> None:
        if not self.connected:
            return
        errors: list[str] = []
        for axis in self._axes:
            try:
                self._exchange(f"stop {axis}")
            except Exception as exc:
                errors.append(f"axis {axis}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def close(self) -> None:
        serial_port = self._serial
        self._axes = ()
        if serial_port is None:
            return
        try:
            serial_port.close()
        except Exception as exc:
            if not getattr(serial_port, "is_open", True):
                self._serial = None
            raise RuntimeError("Unable to release the ANC300 serial port.") from exc
        if getattr(serial_port, "is_open", False):
            raise RuntimeError("The ANC300 serial port remained open after disconnecting.")
        self._serial = None

    def _exchange(self, command: str) -> str:
        if not self.connected:
            raise RuntimeError("ANC300 is not connected.")
        payload = (command.strip() + "\r\n").encode("ascii")
        self._serial.write(payload)
        flush = getattr(self._serial, "flush", None)
        if callable(flush):
            flush()

        deadline = time.monotonic() + 1.5
        lines: list[str] = []
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                if lines:
                    break
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line or line == command:
                continue
            lines.append(line)
            upper = line.upper()
            if upper == "OK" or upper.startswith("ERROR") or line.endswith(">"):
                break
        response = "\n".join(lines).strip()
        if not response:
            raise TimeoutError(f"No response to ANC300 command: {command}")
        if any(line.upper().startswith("ERROR") for line in lines):
            raise RuntimeError(response)
        return response
