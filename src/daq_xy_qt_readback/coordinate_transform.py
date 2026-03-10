"""Coordinate transforms between real-space cursor and hardware voltages."""

from __future__ import annotations

from dataclasses import dataclass
import math


def clamp_voltage(v: float, vmin: float = 0.0, vmax: float = 10.0) -> float:
    return max(vmin, min(vmax, float(v)))


@dataclass(frozen=True)
class MappingSettings:
    """Mapping from real-space (CCD/grid) to hardware outputs."""

    invert_x: bool = False
    invert_y: bool = False
    rotation_enabled: bool = False
    rotation_deg: float = 0.0
    min_v: float = 0.0
    max_v: float = 10.0

    @property
    def center_v(self) -> float:
        return (self.min_v + self.max_v) / 2.0


def _rotate_about_center(x: float, y: float, deg: float, center: float) -> tuple[float, float]:
    if abs(float(deg)) < 1e-12:
        return float(x), float(y)
    th = math.radians(float(deg))
    c = math.cos(th)
    s = math.sin(th)
    xr = float(x) - center
    yr = float(y) - center
    return (c * xr - s * yr + center, s * xr + c * yr + center)


def map_real_to_hw(real_x: float, real_y: float, settings: MappingSettings) -> tuple[float, float]:
    """Map real-space cursor coordinates to hardware voltages."""
    x = float(real_x)
    y = float(real_y)

    # Rotate in real-space axes first.
    if settings.rotation_enabled:
        x, y = _rotate_about_center(x, y, settings.rotation_deg, settings.center_v)

    # Then apply inversion from real-space to increasing-voltage direction.
    if settings.invert_x:
        x = settings.min_v + settings.max_v - x
    if settings.invert_y:
        y = settings.min_v + settings.max_v - y

    return (
        clamp_voltage(x, settings.min_v, settings.max_v),
        clamp_voltage(y, settings.min_v, settings.max_v),
    )


def map_hw_to_real(hw_x: float, hw_y: float, settings: MappingSettings) -> tuple[float, float]:
    """Map hardware voltages back to real-space cursor coordinates."""
    x = clamp_voltage(hw_x, settings.min_v, settings.max_v)
    y = clamp_voltage(hw_y, settings.min_v, settings.max_v)

    # Undo inversion first.
    if settings.invert_x:
        x = settings.min_v + settings.max_v - x
    if settings.invert_y:
        y = settings.min_v + settings.max_v - y

    # Undo rotation to return to real-space axes.
    if settings.rotation_enabled:
        x, y = _rotate_about_center(x, y, -settings.rotation_deg, settings.center_v)

    return (float(x), float(y))


def reachable_real_polygon(settings: MappingSettings) -> list[tuple[float, float]]:
    """Real-space polygon corresponding to full hardware square range."""
    vmin = settings.min_v
    vmax = settings.max_v
    corners_hw = [(vmin, vmin), (vmax, vmin), (vmax, vmax), (vmin, vmax)]
    return [map_hw_to_real(vx, vy, settings) for vx, vy in corners_hw]


def reachable_real_bounds(settings: MappingSettings, margin_ratio: float = 0.05) -> tuple[float, float, float, float]:
    poly = reachable_real_polygon(settings)
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    xmin = min(xs)
    xmax = max(xs)
    ymin = min(ys)
    ymax = max(ys)
    mx = (xmax - xmin) * max(0.0, float(margin_ratio))
    my = (ymax - ymin) * max(0.0, float(margin_ratio))
    return xmin - mx, xmax + mx, ymin - my, ymax + my


def project_real_to_reachable(real_x: float, real_y: float, settings: MappingSettings) -> tuple[float, float]:
    """Project any real-space command to reachable region via hardware clamp."""
    vx, vy = map_real_to_hw(real_x, real_y, settings)
    return map_hw_to_real(vx, vy, settings)
