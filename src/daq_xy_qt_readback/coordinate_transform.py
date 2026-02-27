"""Coordinate transform pipeline for scanner logical axes and AO outputs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any


VALID_POLARITY = {"normal", "inverted"}
VALID_ROTATE_ABOUT = {"origin", "center"}
VALID_OUT_OF_RANGE = {"clamp", "error"}


@dataclass(frozen=True)
class VoltageRange:
    """Common voltage range for both scanner axes."""

    min_v: float = 0.0
    max_v: float = 10.0

    def validate(self) -> None:
        if self.max_v <= self.min_v:
            raise ValueError("voltage_range.max must be greater than voltage_range.min")

    @property
    def center(self) -> float:
        return (self.min_v + self.max_v) / 2.0


@dataclass(frozen=True)
class AxisMap:
    """Map physical scanner axes (u,v) to AO channel names."""

    u: str = "ao0"
    v: str = "ao1"

    def validate(self) -> None:
        if not self.u or not self.v:
            raise ValueError("axis_map.u and axis_map.v must be non-empty channel names")
        if self.u == self.v:
            raise ValueError("axis_map.u and axis_map.v must reference different channels")


@dataclass(frozen=True)
class AxisPolarity:
    """Polarity mapping for physical axes before AO output."""

    u: str = "normal"
    v: str = "normal"

    def validate(self) -> None:
        if self.u not in VALID_POLARITY:
            raise ValueError("axis_polarity.u must be 'normal' or 'inverted'")
        if self.v not in VALID_POLARITY:
            raise ValueError("axis_polarity.v must be 'normal' or 'inverted'")


@dataclass(frozen=True)
class LogicalToPhysicalTransform:
    """Logical-to-physical transform settings."""

    rotation_deg: float = 0.0
    rotate_about: str = "origin"
    offset_u: float = 0.0
    offset_v: float = 0.0

    def validate(self) -> None:
        if self.rotate_about not in VALID_ROTATE_ABOUT:
            raise ValueError("transform.rotate_about must be 'origin' or 'center'")


@dataclass(frozen=True)
class CoordinateSettings:
    """Source-of-truth settings for coordinate conversion."""

    axis_map: AxisMap = AxisMap()
    axis_polarity: AxisPolarity = AxisPolarity()
    voltage_range: VoltageRange = VoltageRange()
    transform: LogicalToPhysicalTransform = LogicalToPhysicalTransform()
    on_out_of_range: str = "clamp"

    def validate(self) -> None:
        self.axis_map.validate()
        self.axis_polarity.validate()
        self.voltage_range.validate()
        self.transform.validate()
        if self.on_out_of_range not in VALID_OUT_OF_RANGE:
            raise ValueError("on_out_of_range must be 'clamp' or 'error'")

    @staticmethod
    def default_for_channels(ch_a: str, ch_b: str) -> "CoordinateSettings":
        return CoordinateSettings(axis_map=AxisMap(u=ch_a, v=ch_b))


def _check_unknown_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(data.keys()) - allowed
    if unknown:
        raise ValueError(f"Unknown keys in {context}: {sorted(unknown)}")


def load_coordinate_settings(path: str | None, default: CoordinateSettings) -> CoordinateSettings:
    """Load coordinate settings from JSON file, or return default when unset."""
    if not path:
        default.validate()
        return default

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return coordinate_settings_from_dict(payload, default)


def coordinate_settings_to_dict(settings: CoordinateSettings) -> dict[str, Any]:
    """Serialize settings to a JSON-compatible dictionary."""
    settings.validate()
    return {
        "axis_map": {"u": settings.axis_map.u, "v": settings.axis_map.v},
        "axis_polarity": {"u": settings.axis_polarity.u, "v": settings.axis_polarity.v},
        "voltage_range": {"min": settings.voltage_range.min_v, "max": settings.voltage_range.max_v},
        "transform": {
            "rotation_deg": settings.transform.rotation_deg,
            "rotate_about": settings.transform.rotate_about,
            "offset_uv": [settings.transform.offset_u, settings.transform.offset_v],
        },
        "on_out_of_range": settings.on_out_of_range,
    }


def coordinate_settings_from_dict(payload: dict[str, Any], default: CoordinateSettings) -> CoordinateSettings:
    """Parse a dictionary into validated coordinate settings."""
    if not isinstance(payload, dict):
        raise ValueError("Coordinate config root must be a JSON object")

    _check_unknown_keys(
        payload,
        {"axis_map", "axis_polarity", "voltage_range", "transform", "on_out_of_range"},
        "root",
    )

    axis_map = default.axis_map
    raw_map = payload.get("axis_map")
    if raw_map is not None:
        if not isinstance(raw_map, dict):
            raise ValueError("axis_map must be an object")
        _check_unknown_keys(raw_map, {"u", "v"}, "axis_map")
        axis_map = AxisMap(u=str(raw_map.get("u", axis_map.u)), v=str(raw_map.get("v", axis_map.v)))

    axis_polarity = default.axis_polarity
    raw_polarity = payload.get("axis_polarity")
    if raw_polarity is not None:
        if not isinstance(raw_polarity, dict):
            raise ValueError("axis_polarity must be an object")
        _check_unknown_keys(raw_polarity, {"u", "v"}, "axis_polarity")
        axis_polarity = AxisPolarity(
            u=str(raw_polarity.get("u", axis_polarity.u)).lower(),
            v=str(raw_polarity.get("v", axis_polarity.v)).lower(),
        )

    voltage_range = default.voltage_range
    raw_range = payload.get("voltage_range")
    if raw_range is not None:
        if not isinstance(raw_range, dict):
            raise ValueError("voltage_range must be an object")
        _check_unknown_keys(raw_range, {"min", "max"}, "voltage_range")
        voltage_range = VoltageRange(
            min_v=float(raw_range.get("min", voltage_range.min_v)),
            max_v=float(raw_range.get("max", voltage_range.max_v)),
        )

    transform = default.transform
    raw_transform = payload.get("transform")
    if raw_transform is not None:
        if not isinstance(raw_transform, dict):
            raise ValueError("transform must be an object")
        _check_unknown_keys(raw_transform, {"rotation_deg", "rotate_about", "offset_uv"}, "transform")
        offset_u, offset_v = transform.offset_u, transform.offset_v
        if "offset_uv" in raw_transform:
            raw_offset = raw_transform["offset_uv"]
            if (
                not isinstance(raw_offset, list)
                or len(raw_offset) != 2
            ):
                raise ValueError("transform.offset_uv must be [u0, v0]")
            offset_u = float(raw_offset[0])
            offset_v = float(raw_offset[1])
        transform = LogicalToPhysicalTransform(
            rotation_deg=float(raw_transform.get("rotation_deg", transform.rotation_deg)),
            rotate_about=str(raw_transform.get("rotate_about", transform.rotate_about)).lower(),
            offset_u=offset_u,
            offset_v=offset_v,
        )

    settings = CoordinateSettings(
        axis_map=axis_map,
        axis_polarity=axis_polarity,
        voltage_range=voltage_range,
        transform=transform,
        on_out_of_range=str(payload.get("on_out_of_range", default.on_out_of_range)).lower(),
    )
    settings.validate()
    return settings


def logical_xy_to_physical_uv(x: float, y: float, settings: CoordinateSettings) -> tuple[float, float]:
    """Transform logical (x,y) to physical scanner axes (u,v)."""
    theta = math.radians(settings.transform.rotation_deg)
    c = math.cos(theta)
    s = math.sin(theta)

    cx = 0.0
    cy = 0.0
    if settings.transform.rotate_about == "center":
        cx = settings.voltage_range.center
        cy = settings.voltage_range.center

    xr = x - cx
    yr = y - cy
    u = c * xr - s * yr + cx + settings.transform.offset_u
    v = s * xr + c * yr + cy + settings.transform.offset_v
    return u, v


def physical_uv_to_logical_xy(u: float, v: float, settings: CoordinateSettings) -> tuple[float, float]:
    """Inverse transform: physical scanner axes (u,v) to logical (x,y)."""
    theta = -math.radians(settings.transform.rotation_deg)
    c = math.cos(theta)
    s = math.sin(theta)

    cx = 0.0
    cy = 0.0
    if settings.transform.rotate_about == "center":
        cx = settings.voltage_range.center
        cy = settings.voltage_range.center

    ur = u - settings.transform.offset_u - cx
    vr = v - settings.transform.offset_v - cy
    x = c * ur - s * vr + cx
    y = s * ur + c * vr + cy
    return x, y


def _apply_polarity(axis_value: float, polarity: str, vmin: float, vmax: float) -> float:
    if polarity == "normal":
        return axis_value
    return vmin + vmax - axis_value


def _remove_polarity(voltage_value: float, polarity: str, vmin: float, vmax: float) -> float:
    if polarity == "normal":
        return voltage_value
    return vmin + vmax - voltage_value


def _handle_range(value: float, axis_name: str, settings: CoordinateSettings) -> tuple[float, bool]:
    vmin = settings.voltage_range.min_v
    vmax = settings.voltage_range.max_v
    if vmin <= value <= vmax:
        return value, False
    if settings.on_out_of_range == "error":
        raise ValueError(f"{axis_name}={value:.6f} out of range [{vmin}, {vmax}]")
    return max(vmin, min(vmax, value)), True


def physical_uv_to_ao_volts(u: float, v: float, settings: CoordinateSettings) -> tuple[dict[str, float], bool]:
    """Map physical axes to AO channels and voltages."""
    u_bounded, u_clamped = _handle_range(u, "u", settings)
    v_bounded, v_clamped = _handle_range(v, "v", settings)
    vmin = settings.voltage_range.min_v
    vmax = settings.voltage_range.max_v
    volts_u = _apply_polarity(u_bounded, settings.axis_polarity.u, vmin, vmax)
    volts_v = _apply_polarity(v_bounded, settings.axis_polarity.v, vmin, vmax)
    ao_volts = {
        settings.axis_map.u: volts_u,
        settings.axis_map.v: volts_v,
    }
    return ao_volts, (u_clamped or v_clamped)


def ao_volts_to_physical_uv(ao_volts: dict[str, float], settings: CoordinateSettings) -> tuple[float, float]:
    """Inverse mapping: AO channel voltages to physical scanner axes."""
    if settings.axis_map.u not in ao_volts or settings.axis_map.v not in ao_volts:
        raise ValueError("AO voltage map does not contain both configured channels")
    vmin = settings.voltage_range.min_v
    vmax = settings.voltage_range.max_v
    u = _remove_polarity(float(ao_volts[settings.axis_map.u]), settings.axis_polarity.u, vmin, vmax)
    v = _remove_polarity(float(ao_volts[settings.axis_map.v]), settings.axis_polarity.v, vmin, vmax)
    return u, v
