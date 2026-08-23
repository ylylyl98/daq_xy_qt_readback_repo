"""PyQt6 NI-DAQ XY control with explicit real-space vs hardware mapping."""

from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import sys
from typing import Any

from PyQt6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .anc300_positioner import (
    ANC300Positioner,
    PositionerSettings,
    list_serial_ports,
    load_positioner_settings,
    save_positioner_settings,
)
from ._version import __version__
from .update_checker import (
    ReleaseInfo,
    UpdatePreferences,
    fetch_latest_release,
    is_newer_release,
    load_update_preferences,
    save_update_preferences,
)

from .coordinate_transform import (
    MappingSettings,
    clamp_voltage,
    map_hw_to_real,
    map_real_to_hw,
    project_real_to_reachable,
    reachable_real_bounds,
    reachable_real_polygon,
)

STEP_PER_MOVE = 0.05
LOGGER = logging.getLogger(__name__)
APP_DISPLAY_NAME = "DAQ XY Control"
APP_USER_MODEL_ID = "daq_xy_qt_readback.DAQXYControl"
APP_MUTEX_NAME = "DAQXYControl.Application.8E67D61C"
_ASSET_FILES = ExitStack()
_WINDOWS_APP_MUTEX_HANDLE: Any | None = None
V_MIN_DEFAULT = 0.0
V_MAX_DEFAULT = 10.0


def _centered_top_left(available_geometry: QRect, window_size: QSize) -> QPoint:
    """Return the top-left point that centers a window in a screen's usable area."""
    return QPoint(
        available_geometry.left() + (available_geometry.width() - window_size.width()) // 2,
        available_geometry.top() + (available_geometry.height() - window_size.height()) // 2,
    )


def _asset_file_path(filename: str) -> str | None:
    """Return a filesystem path for a bundled asset when available."""
    package = __package__ or "daq_xy_qt_readback"
    try:
        asset = resources.files(package).joinpath("assets").joinpath(filename)
        if not asset.is_file():
            return None
        return str(_ASSET_FILES.enter_context(resources.as_file(asset)))
    except Exception as exc:
        LOGGER.debug("Unable to resolve bundled asset %s: %s", filename, exc)
        return None


def _load_app_icon() -> QIcon:
    for filename in (
        "daq_xy_control_unique.ico",
        "daq_xy_control_unique.svg",
        "daq_xy_icon.ico",
        "daq_xy_icon.svg",
    ):
        path = _asset_file_path(filename)
        if not path:
            continue
        icon = QIcon(path)
        if not icon.isNull():
            return icon
        LOGGER.debug("Bundled icon could not be loaded: %s", path)
    return QIcon()


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_USER_MODEL_ID))
    except Exception as exc:
        LOGGER.debug("Unable to set Windows AppUserModelID: %s", exc)


def _create_windows_app_mutex() -> None:
    """Expose a process-lifetime mutex so the installer will not update a running app."""
    global _WINDOWS_APP_MUTEX_HANDLE
    if sys.platform != "win32" or _WINDOWS_APP_MUTEX_HANDLE is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
        if handle:
            _WINDOWS_APP_MUTEX_HANDLE = handle
    except Exception as exc:
        LOGGER.debug("Unable to create installer coordination mutex: %s", exc)


def _set_windows_native_window_icon(window: QWidget) -> None:
    if sys.platform != "win32":
        return
    icon_path = _asset_file_path("daq_xy_control_unique.ico") or _asset_file_path("daq_xy_icon.ico")
    if not icon_path:
        return
    try:
        import ctypes
        from ctypes import wintypes

        image_icon = 1
        load_from_file = 0x00000010
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

        hwnd = wintypes.HWND(int(window.winId()))
        small_icon = user32.LoadImageW(None, icon_path, image_icon, 16, 16, load_from_file)
        big_icon = user32.LoadImageW(None, icon_path, image_icon, 32, 32, load_from_file)
        if not small_icon and not big_icon:
            return

        previous_handles = list(getattr(window, "_daq_xy_windows_icon_handles", []))
        new_handles = []
        if small_icon:
            user32.SendMessageW(hwnd, wm_seticon, icon_small, small_icon)
            new_handles.append(small_icon)
        if big_icon:
            user32.SendMessageW(hwnd, wm_seticon, icon_big, big_icon)
            new_handles.append(big_icon)
        setattr(window, "_daq_xy_windows_icon_handles", new_handles)

        if previous_handles:
            user32.DestroyIcon.restype = wintypes.BOOL
            user32.DestroyIcon.argtypes = [wintypes.HANDLE]
            for handle in previous_handles:
                if handle:
                    user32.DestroyIcon(handle)
    except Exception as exc:
        LOGGER.debug("Unable to set native Windows taskbar icon: %s", exc)


def _release_windows_native_window_icon(window: QWidget) -> None:
    if sys.platform != "win32":
        return
    handles = list(getattr(window, "_daq_xy_windows_icon_handles", []))
    if not handles:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        for handle in handles:
            if handle:
                user32.DestroyIcon(handle)
    except Exception:
        LOGGER.debug("Unable to release native Windows icon handles.", exc_info=True)
    setattr(window, "_daq_xy_windows_icon_handles", [])


def _apply_window_icon(window: QWidget) -> None:
    icon = _load_app_icon()
    if not icon.isNull():
        window.setWindowIcon(icon)
    _set_windows_native_window_icon(window)


def _configure_application(app: QApplication) -> None:
    _set_windows_app_user_model_id()
    _create_windows_app_mutex()
    app.setOrganizationName("Instrument Control")
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_USER_MODEL_ID)
    icon = _load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)


def _qt_application() -> QApplication:
    _set_windows_app_user_model_id()
    app = QApplication.instance() or QApplication(sys.argv)
    _configure_application(app)
    return app

try:
    from iv_automation import DaqControl as _RealDaqControl
    _DAQ_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    _RealDaqControl = None
    _DAQ_IMPORT_ERROR = exc


def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return clamp_voltage(v, lo, hi)


def _next_ramp_point(
    x: float,
    y: float,
    target_x: float,
    target_y: float,
    step: float,
) -> tuple[float, float]:
    dx = target_x - x
    dy = target_y - y
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= step:
        return target_x, target_y
    return x + (dx / dist) * step, y + (dy / dist) * step


def _default_data_dir() -> Path:
    base = os.environ.get("DAQ_XY_DATA_DIR")
    if base:
        return Path(base)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "daq_xy_qt_readback"
    return Path.home() / ".daq_xy_qt_readback"


def _prefs_path() -> Path:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "ui_mapping_settings.json"


def _positioner_prefs_path() -> Path:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "positioner_settings.json"


def _update_prefs_path() -> Path:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "update_settings.json"


@dataclass
class PersistedMapping:
    selected_device_name: str = ""
    x_channel: str = "ao0"
    y_channel: str = "ao1"
    invert_x: bool = False
    invert_y: bool = False
    rotation_enabled: bool = False
    rotation_deg: float = 0.0

    def to_mapping_settings(self) -> MappingSettings:
        return MappingSettings(
            invert_x=bool(self.invert_x),
            invert_y=bool(self.invert_y),
            rotation_enabled=bool(self.rotation_enabled),
            rotation_deg=float(self.rotation_deg),
        )


def _load_persisted_mapping() -> PersistedMapping:
    path = _prefs_path()
    if not path.exists():
        return PersistedMapping()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return PersistedMapping()
    if not isinstance(payload, dict):
        return PersistedMapping()
    return PersistedMapping(
        selected_device_name=str(payload.get("selected_device_name", "")),
        x_channel=str(payload.get("x_channel", "ao0")),
        y_channel=str(payload.get("y_channel", "ao1")),
        invert_x=bool(payload.get("invert_x", False)),
        invert_y=bool(payload.get("invert_y", False)),
        rotation_enabled=bool(payload.get("rotation_enabled", False)),
        rotation_deg=float(payload.get("rotation_deg", 0.0)),
    )


def _save_persisted_mapping(mapping: PersistedMapping) -> None:
    _prefs_path().write_text(
        json.dumps(
            {
                "selected_device_name": mapping.selected_device_name,
                "x_channel": mapping.x_channel,
                "y_channel": mapping.y_channel,
                "invert_x": bool(mapping.invert_x),
                "invert_y": bool(mapping.invert_y),
                "rotation_enabled": bool(mapping.rotation_enabled),
                "rotation_deg": float(mapping.rotation_deg),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _detect_devices_and_channels() -> tuple[list[str], dict[str, list[str]], str | None]:
    try:
        import nidaqmx.system
    except Exception as exc:
        return [], {}, f"nidaqmx import failed: {exc}"
    try:
        system = nidaqmx.system.System.local()
        by_device: dict[str, list[str]] = {}
        names: list[str] = []
        for dev in list(system.devices):
            name = str(getattr(dev, "name", "")).strip()
            if not name:
                continue
            names.append(name)
            ao_names: list[str] = []
            try:
                for ch in list(dev.ao_physical_chans):
                    ch_name = str(getattr(ch, "name", ""))
                    ao_names.append(ch_name.split("/")[-1].strip())
            except Exception:
                ao_names = []
            by_device[name] = [x for x in ao_names if x]
        return names, by_device, None
    except Exception as exc:
        return [], {}, f"device enumeration failed: {exc}"


class DaqInterface:
    """Real NI-DAQ wrapper that preserves live AO state until an explicit move."""

    def __init__(self, dev_name: str, ao_x: str, ao_y: str) -> None:
        if _RealDaqControl is None:
            raise RuntimeError(
                "Real DAQ control unavailable. Install iv_automation.py dependencies "
                "(nidaqmx, pyvisa, numpy)."
            ) from _DAQ_IMPORT_ERROR
        self.dev_name = dev_name
        self.ao_x = ao_x
        self.ao_y = ao_y
        self._daq = _RealDaqControl(dev_name)
        self._name_x = "x_v"
        self._name_y = "y_v"
        self._vx = 0.0
        self._vy = 0.0
        self._readback_uncertain = False
        self._readback_status = "AO state not read yet."
        self._readback_warning_logged = False
        try:
            self._daq.add_ao_channel(ao_x, self._name_x)
            self._daq.add_ao_channel(ao_y, self._name_y)
            self._vx, self._vy = self._preserve_existing_outputs_on_connect()
            self._cache_outputs_without_writing(self._vx, self._vy)
        except Exception:
            self.close()
            raise

    @property
    def readback_uncertain(self) -> bool:
        return bool(self._readback_uncertain)

    @property
    def readback_status(self) -> str:
        return str(self._readback_status)

    def _cache_outputs_without_writing(self, vx: float, vy: float) -> None:
        # Keep the driver-side command cache aligned with the preserved live outputs,
        # but do not issue any AO write during startup/shutdown/reconnect.
        self._vx = clamp_voltage(vx)
        self._vy = clamp_voltage(vy)
        self._daq.receive_x(self._name_x, self._vx)
        self._daq.receive_x(self._name_y, self._vy)

    def _read_backend_cached_outputs(self) -> tuple[float, float] | None:
        try:
            x_vals = getattr(self._daq, "x_values", None)
            x_indexes = getattr(self._daq, "x_indexes", {})
            if x_vals is None:
                return None
            vx = float(x_vals[x_indexes[self._name_x]])
            vy = float(x_vals[x_indexes[self._name_y]])
            return clamp_voltage(vx), clamp_voltage(vy)
        except Exception:
            return None

    def _read_hardware_outputs(self) -> tuple[float, float]:
        self._daq.read_y()
        mx = float(self._daq.send_y("measured_" + self._name_x))
        my = float(self._daq.send_y("measured_" + self._name_y))
        return clamp_voltage(mx), clamp_voltage(my)

    def _preserve_existing_outputs_on_connect(self) -> tuple[float, float]:
        try:
            vx, vy = self._read_hardware_outputs()
            self._readback_uncertain = False
            self._readback_status = "Preserved existing scanner AO outputs from hardware readback."
            LOGGER.info(
                "Preserved existing scanner AO outputs on connect for %s (%s,%s): %.4f V, %.4f V",
                self.dev_name,
                self.ao_x,
                self.ao_y,
                vx,
                vy,
            )
            return vx, vy
        except Exception as exc:
            cached = self._read_backend_cached_outputs()
            if cached is not None:
                vx, vy = cached
                self._readback_uncertain = True
                self._readback_status = (
                    "Hardware AO readback is unavailable; preserving the last known driver cache. "
                    "No startup write was issued."
                )
                LOGGER.warning(
                    "AO readback unavailable while connecting to %s (%s,%s); preserving cached outputs %.4f V, %.4f V without writing (%s).",
                    self.dev_name,
                    self.ao_x,
                    self.ao_y,
                    vx,
                    vy,
                    exc,
                )
                self._readback_warning_logged = True
                return vx, vy
            raise RuntimeError(
                f"Unable to determine existing scanner AO outputs for {self.dev_name} "
                f"({self.ao_x},{self.ao_y}) safely. No startup write was issued."
            ) from exc

    def read_outputs(self) -> tuple[float, float]:
        try:
            self._vx, self._vy = self._read_hardware_outputs()
            self._readback_uncertain = False
            self._readback_status = "Showing live hardware AO readback."
            self._readback_warning_logged = False
        except Exception as exc:
            cached = self._read_backend_cached_outputs()
            if cached is not None:
                self._vx, self._vy = cached
            self._cache_outputs_without_writing(self._vx, self._vy)
            self._readback_uncertain = True
            self._readback_status = (
                "Hardware AO readback is unavailable; showing the last preserved output values. "
                "No automatic AO write was issued."
            )
            if not self._readback_warning_logged:
                LOGGER.warning(
                    "Hardware AO readback unavailable for %s (%s,%s); preserving last known outputs %.4f V, %.4f V without writing (%s).",
                    self.dev_name,
                    self.ao_x,
                    self.ao_y,
                    self._vx,
                    self._vy,
                    exc,
                )
                self._readback_warning_logged = True
            else:
                LOGGER.debug("Hardware AO readback still unavailable; preserved outputs remain cached.")
        return self._vx, self._vy

    def write_outputs(self, vx: float, vy: float) -> tuple[float, float]:
        self._cache_outputs_without_writing(vx, vy)
        self._daq.write_x()
        return self.read_outputs()

    def close(self) -> None:
        LOGGER.info(
            "Closing DAQ interface for %s (%s,%s) without altering scanner AO outputs.",
            self.dev_name,
            self.ao_x,
            self.ao_y,
        )
        try:
            self._daq.ao_task.close()
        except Exception:
            LOGGER.debug("Failed closing AO task.", exc_info=True)
        try:
            self._daq.ai_task.close()
        except Exception:
            LOGGER.debug("Failed closing AI task.", exc_info=True)


class DemoDaqInterface:
    """No-hardware backend used only if real DAQ startup fails."""

    def __init__(self) -> None:
        self._vx = 0.0
        self._vy = 0.0

    def read_outputs(self) -> tuple[float, float]:
        return self._vx, self._vy

    def write_outputs(self, vx: float, vy: float) -> tuple[float, float]:
        self._vx = clamp_voltage(vx)
        self._vy = clamp_voltage(vy)
        return self._vx, self._vy

    def close(self) -> None:
        return


class XyPad(QWidget):
    """Real-space display (CCD motion axes)."""

    clicked = pyqtSignal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rx = 0.0
        self._ry = 0.0
        self._target_rx = 0.0
        self._target_ry = 0.0
        self._xmin = 0.0
        self._xmax = 10.0
        self._ymin = 0.0
        self._ymax = 10.0
        self._view_xmin = 0.0
        self._view_xmax = 10.0
        self._view_ymin = 0.0
        self._view_ymax = 10.0
        self._boundary_real: list[tuple[float, float]] = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        self._rotation_enabled = False
        self._rotation_deg = 0.0

    def set_real_xy(self, rx: float, ry: float) -> None:
        self._rx = float(rx)
        self._ry = float(ry)
        self.update()

    def set_target_real_xy(self, rx: float, ry: float) -> None:
        self._target_rx = float(rx)
        self._target_ry = float(ry)
        self.update()

    def set_rotation(self, enabled: bool, deg: float) -> None:
        self._rotation_enabled = bool(enabled)
        self._rotation_deg = float(deg)
        self.update()

    def set_voltage_range(self, xmin: float, xmax: float, ymin: float, ymax: float) -> None:
        self._xmin = float(xmin)
        self._xmax = float(xmax)
        self._ymin = float(ymin)
        self._ymax = float(ymax)
        self._rx = float(self._rx)
        self._ry = float(self._ry)
        self.update()

    def set_view_bounds(self, xmin: float, xmax: float, ymin: float, ymax: float) -> None:
        self._view_xmin = float(xmin)
        self._view_xmax = float(xmax)
        self._view_ymin = float(ymin)
        self._view_ymax = float(ymax)
        self.update()

    def set_boundary_polygon(self, points: list[tuple[float, float]]) -> None:
        self._boundary_real = list(points)
        self.update()

    def _plot_rect(self) -> tuple[float, float, float]:
        side = float(max(40, min(self.width(), self.height()) - 20))
        x0 = (self.width() - side) / 2.0
        y0 = (self.height() - side) / 2.0
        return x0, y0, side

    def _to_plot(self, rx: float, ry: float) -> tuple[float, float]:
        x0, y0, side = self._plot_rect()
        span_x = max(1e-9, self._view_xmax - self._view_xmin)
        span_y = max(1e-9, self._view_ymax - self._view_ymin)
        px = x0 + ((float(rx) - self._view_xmin) / span_x) * (side - 1.0)
        py = y0 + (1.0 - ((float(ry) - self._view_ymin) / span_y)) * (side - 1.0)
        return px, py

    def _from_plot(self, px: float, py: float) -> tuple[float, float]:
        x0, y0, side = self._plot_rect()
        span_x = max(1e-9, self._view_xmax - self._view_xmin)
        span_y = max(1e-9, self._view_ymax - self._view_ymin)
        rx = self._view_xmin + ((float(px) - x0) / max(1.0, side - 1.0)) * span_x
        ry = self._view_ymin + (1.0 - ((float(py) - y0) / max(1.0, side - 1.0))) * span_y
        return rx, ry

    def mousePressEvent(self, e: Any) -> None:  # type: ignore[override]
        self._handle_mouse(e)

    def mouseMoveEvent(self, e: Any) -> None:  # type: ignore[override]
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._handle_mouse(e)

    def _handle_mouse(self, e: Any) -> None:
        pos = e.position()
        rx, ry = self._from_plot(float(pos.x()), float(pos.y()))
        self.clicked.emit(float(rx), float(ry))

    def paintEvent(self, _: Any) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        x0, y0, side = self._plot_rect()
        x1 = x0 + side - 1.0
        y1 = y0 + side - 1.0

        p.fillRect(self.rect(), QColor("#f8fafc"))
        plot_rect = QRectF(x0, y0, side - 1.0, side - 1.0)
        p.setPen(QPen(QColor("#cbd5e1"), 1.2))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(plot_rect, 12.0, 12.0)

        grid_pen = QPen(QColor("#e2e8f0"), 1)
        p.setPen(grid_pen)
        for i in range(1, 5):
            xi = x0 + i * (side / 5.0)
            yi = y0 + i * (side / 5.0)
            p.drawLine(int(xi), int(y0), int(xi), int(y1))
            p.drawLine(int(x0), int(yi), int(x1), int(yi))

        # Reachable region boundary in real-space (mapped from hardware square).
        if len(self._boundary_real) >= 2:
            boundary_path = QPainterPath()
            sx, sy = self._to_plot(*self._boundary_real[0])
            boundary_path.moveTo(QPointF(sx, sy))
            for i in range(len(self._boundary_real)):
                ax, ay = self._boundary_real[i]
                bx, by = self._boundary_real[(i + 1) % len(self._boundary_real)]
                p1x, p1y = self._to_plot(ax, ay)
                p2x, p2y = self._to_plot(bx, by)
                if i > 0:
                    boundary_path.lineTo(QPointF(p1x, p1y))
                boundary_path.lineTo(QPointF(p2x, p2y))
            boundary_path.closeSubpath()
            fill = QColor("#38bdf8")
            fill.setAlpha(30)
            p.fillPath(boundary_path, fill)
            boundary_pen = QPen(QColor("#0284c7"), 2)
            boundary_pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(boundary_pen)
            p.drawPath(boundary_path)

        # Fixed display axes in real-space frame.
        cxp, cyp = self._to_plot((self._view_xmin + self._view_xmax) * 0.5, (self._view_ymin + self._view_ymax) * 0.5)
        axis_len = side * 0.22
        x_tip = (cxp + axis_len, cyp)
        y_tip = (cxp, cyp - axis_len)
        p.setPen(QPen(QColor("#64748b"), 1.6))
        p.drawLine(int(cxp), int(cyp), int(x_tip[0]), int(x_tip[1]))
        p.drawLine(int(cxp), int(cyp), int(y_tip[0]), int(y_tip[1]))
        p.drawLine(int(x_tip[0]), int(x_tip[1]), int(x_tip[0] - 8), int(x_tip[1] - 4))
        p.drawLine(int(x_tip[0]), int(x_tip[1]), int(x_tip[0] - 8), int(x_tip[1] + 4))
        p.drawLine(int(y_tip[0]), int(y_tip[1]), int(y_tip[0] - 4), int(y_tip[1] + 8))
        p.drawLine(int(y_tip[0]), int(y_tip[1]), int(y_tip[0] + 4), int(y_tip[1] + 8))
        p.drawText(int(x_tip[0] + 6), int(x_tip[1] + 4), "X")
        p.drawText(int(y_tip[0] + 6), int(y_tip[1] + 4), "Y")

        # Target setpoint in real-space.
        tx, ty = self._to_plot(self._target_rx, self._target_ry)
        target_pen = QPen(QColor("#f59e0b"), 2)
        target_pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(target_pen)
        p.drawLine(int(tx - 8), int(ty), int(tx + 8), int(ty))
        p.drawLine(int(tx), int(ty - 8), int(tx), int(ty + 8))
        p.setBrush(QColor("#fef3c7"))
        p.drawEllipse(QPointF(tx, ty), 4.0, 4.0)

        # Cursor (real-space position)
        px, py = self._to_plot(self._rx, self._ry)
        p.setPen(QPen(QColor("#0f172a"), 2.5))
        p.setBrush(QColor("#22c55e"))
        p.drawEllipse(QPointF(px, py), 8.0, 8.0)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(int(px - 5), int(py), int(px + 5), int(py))
        p.drawLine(int(px), int(py - 5), int(px), int(py + 5))

        # Range labels.
        label_font = QFont(p.font())
        label_font.setPointSize(8)
        p.setFont(label_font)
        p.setPen(QPen(QColor("#64748b"), 1))
        p.drawText(int(x0) - 4, int(y1) + 16, f"{self._view_xmin:.2f}")
        p.drawText(int(x1) - 44, int(y1) + 16, f"{self._view_xmax:.2f}")
        p.drawText(int(x0) - 44, int(y1), f"{self._view_ymin:.2f}")
        p.drawText(int(x0) - 44, int(y0) + 10, f"{self._view_ymax:.2f}")

        if not self.isEnabled():
            veil = QColor("#f8fafc")
            veil.setAlpha(150)
            p.fillRect(self.rect(), veil)


@dataclass
class RampConfig:
    step_v: float = STEP_PER_MOVE
    dwell_ms: int = 100


class _PositionerWorker(QObject):
    connected = pyqtSignal(str)
    disconnected = pyqtSignal(str)
    operation_started = pyqtSignal(str)
    operation_finished = pyqtSignal(str)
    failed = pyqtSignal(str)
    shutdown_finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._positioner = ANC300Positioner()

    @pyqtSlot(object)
    def connect_device(self, settings: object) -> None:
        try:
            assert isinstance(settings, PositionerSettings)
            self.operation_started.emit("Connecting")
            version = self._positioner.connect(settings)
            self.connected.emit(version)
            self.operation_finished.emit("Connected")
        except Exception as exc:
            self.failed.emit(str(exc))

    @pyqtSlot()
    def disconnect_device(self) -> None:
        self._positioner.close()
        self.disconnected.emit("Disconnected")

    @pyqtSlot(object, str, str, int)
    def move(self, settings: object, axis: str, direction: str, steps: int) -> None:
        try:
            assert isinstance(settings, PositionerSettings)
            self.operation_started.emit(f"Moving {direction}")
            self._positioner.move(settings, axis, direction, steps)
            self.operation_finished.emit(f"Moved {direction} {steps} step(s)")
        except Exception as exc:
            self._positioner.close()
            self.failed.emit(str(exc))

    @pyqtSlot()
    def stop_all(self) -> None:
        try:
            self.operation_started.emit("Stopping")
            self._positioner.stop_all()
            self.operation_finished.emit("STOP sent to all configured axes")
        except Exception as exc:
            self._positioner.close()
            self.failed.emit(str(exc))

    @pyqtSlot()
    def shutdown(self) -> None:
        self._positioner.close()
        self.shutdown_finished.emit()
        thread = self.thread()
        if thread is not None:
            thread.quit()


class _UpdateWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    @pyqtSlot()
    def check(self) -> None:
        try:
            self.finished.emit(fetch_latest_release())
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            thread = self.thread()
            if thread is not None:
                thread.quit()


class DaqXYWindow(QMainWindow):
    """Main UI keeping hardware outputs and real-space display separated."""

    _positioner_connect_requested = pyqtSignal(object)
    _positioner_disconnect_requested = pyqtSignal()
    _positioner_move_requested = pyqtSignal(object, str, str, int)
    _positioner_stop_requested = pyqtSignal()
    _positioner_shutdown_requested = pyqtSignal()

    def __init__(
        self,
        dev_name: str,
        ao_x: str,
        ao_y: str,
        mapping: MappingSettings,
        devices: list[str],
        channels_by_device: dict[str, list[str]],
        demo_reason: str | None = None,
    ) -> None:
        super().__init__()
        self._full_window_title = f"NI-DAQ XY Control - Ramped {STEP_PER_MOVE}V @ 100ms"
        self.setWindowTitle(self._full_window_title)
        _apply_window_icon(self)
        self._demo_reason = demo_reason
        self._debug_mapping = os.environ.get("DAQ_XY_DEBUG_MAPPING", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._devices = list(devices)
        self._channels_by_device = dict(channels_by_device)
        self._selected_device = dev_name
        self._ao_x = ao_x
        self._ao_y = ao_y
        self._mapping = mapping
        self._enabled = False
        self._syncing = False
        self._mapping_dirty = False
        self._readback_uncertain = False
        self._readback_status = ""
        self._vmin = V_MIN_DEFAULT
        self._vmax = V_MAX_DEFAULT
        self._positioner_settings = load_positioner_settings(_positioner_prefs_path())
        self._positioner_connected = False
        self._positioner_busy = False
        self._positioner_version = ""
        self._update_preferences = load_update_preferences(_update_prefs_path())
        self._available_release: ReleaseInfo | None = None
        self._update_check_manual = False
        self._update_thread: QThread | None = None
        self._update_worker: _UpdateWorker | None = None
        self._compact_mode = False
        self._full_window_geometry: Any | None = None
        self._full_window_flags: Any | None = None
        self._full_window_minimum_size: QSize | None = None
        self._full_window_maximum_size: QSize | None = None

        if demo_reason:
            self._daq: DaqInterface | DemoDaqInterface = DemoDaqInterface()
        else:
            self._daq = DaqInterface(dev_name, ao_x, ao_y)

        self._pull_outputs_from_daq()
        self._rx, self._ry = map_hw_to_real(self._vx, self._vy, self._mapping)
        self._target_vx = self._vx
        self._target_vy = self._vy
        self._target_rx = self._rx
        self._target_ry = self._ry

        self.ramp = RampConfig()
        self._ramp_timer = QTimer(self)
        self._ramp_timer.timeout.connect(self._ramp_step)

        self._build_ui()
        self._populate_mapping_controls()
        self._populate_positioner_controls()
        self._start_positioner_worker()
        self._sync_ui()
        self._apply_demo_mode_if_needed()
        if self._automatic_update_checks_enabled() and not self._update_preferences.checked_recently():
            QTimer.singleShot(2000, self._start_automatic_update_check)

    def _start_positioner_worker(self) -> None:
        self._positioner_thread = QThread(self)
        self._positioner_worker = _PositionerWorker()
        self._positioner_worker.moveToThread(self._positioner_thread)
        self._positioner_connect_requested.connect(self._positioner_worker.connect_device)
        self._positioner_disconnect_requested.connect(self._positioner_worker.disconnect_device)
        self._positioner_move_requested.connect(self._positioner_worker.move)
        self._positioner_stop_requested.connect(self._positioner_worker.stop_all)
        self._positioner_shutdown_requested.connect(self._positioner_worker.shutdown)
        self._positioner_worker.connected.connect(self._on_positioner_connected)
        self._positioner_worker.disconnected.connect(self._on_positioner_disconnected)
        self._positioner_worker.operation_started.connect(self._on_positioner_operation_started)
        self._positioner_worker.operation_finished.connect(self._on_positioner_operation_finished)
        self._positioner_worker.failed.connect(self._on_positioner_failed)
        self._positioner_worker.shutdown_finished.connect(self._positioner_thread.quit)
        self._positioner_thread.finished.connect(self._positioner_worker.deleteLater)
        self._positioner_thread.start()

    def _build_ui(self) -> None:
        self._apply_modern_style()

        self._view_stack = QStackedWidget()
        self.setCentralWidget(self._view_stack)

        cw = QWidget()
        cw.setObjectName("appRoot")
        root = QVBoxLayout(cw)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(14)

        root.addWidget(self._build_command_bar())
        root.addWidget(self._build_update_banner())

        workspace = QHBoxLayout()
        workspace.setSpacing(14)
        workspace.addWidget(self._build_xy_panel(), 5)
        workspace.addWidget(self._build_control_stack(), 3)
        root.addLayout(workspace, 1)

        root.addWidget(self._build_status_bar())
        self._view_stack.addWidget(cw)
        self._full_page = cw

        self._compact_page = self._build_compact_page()
        self._view_stack.addWidget(self._compact_page)
        self._view_stack.setCurrentWidget(self._full_page)
        self._connect_controls()
        self._build_compact_shortcuts()

    def _build_command_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("commandBar")
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        title_stack = QVBoxLayout()
        title_stack.setSpacing(1)
        title = QLabel(APP_DISPLAY_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel(f"Ramped output: {STEP_PER_MOVE:.2f} V every 100 ms")
        subtitle.setObjectName("appSubtitle")
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        layout.addLayout(title_stack, 1)

        self.lbl_output_chip = self._chip("OFF", "off")
        self.lbl_output_chip.setToolTip("Output state")
        self.lbl_readback_chip = self._chip("--", "neutral")
        self.lbl_readback_chip.setToolTip("Readback state")
        self.lbl_device_chip = self._chip(self._selected_device or "--", "neutral")
        self.lbl_device_chip.setToolTip("Active device")
        layout.addWidget(self.lbl_output_chip)
        layout.addWidget(self.lbl_readback_chip)
        layout.addWidget(self.lbl_device_chip)

        self.chk_enable = QCheckBox("Output")
        self.chk_enable.setToolTip("Enable ramped DAQ output writes.")
        self.chk_enable.setObjectName("enableOutput")
        self.btn_home = QPushButton("Home")
        self.btn_home.setToolTip("Move to real-space center (5.0, 5.0).")
        self.btn_home.setProperty("role", "secondary")
        self.btn_ground = QPushButton("Ground")
        self.btn_ground.setToolTip("Ramp hardware outputs to 0 V.")
        self.btn_ground.setProperty("role", "danger")
        self.btn_compact = QPushButton("Compact")
        self.btn_compact.setToolTip("Show only the directional controller and keep it above other windows.")
        self.btn_compact.setProperty("role", "secondary")
        self.btn_about = QPushButton("About")
        self.btn_about.setToolTip("Show the installed version and check for updates.")
        self.btn_about.setProperty("role", "secondary")
        layout.addWidget(self.chk_enable)
        layout.addWidget(self.btn_home)
        layout.addWidget(self.btn_ground)
        layout.addWidget(self.btn_compact)
        layout.addWidget(self.btn_about)
        return bar

    def _build_update_banner(self) -> QWidget:
        banner = QFrame()
        banner.setObjectName("updateBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        self.lbl_update_available = QLabel("")
        self.lbl_update_available.setWordWrap(True)
        self.btn_view_update = QPushButton("View update")
        self.btn_view_update.setProperty("role", "primary")
        self.btn_update_later = QPushButton("Later")
        self.btn_update_later.setProperty("role", "secondary")
        self.btn_skip_update = QPushButton("Skip this version")
        self.btn_skip_update.setProperty("role", "secondary")
        layout.addWidget(self.lbl_update_available, 1)
        layout.addWidget(self.btn_view_update)
        layout.addWidget(self.btn_update_later)
        layout.addWidget(self.btn_skip_update)
        banner.setVisible(False)
        self.update_banner = banner
        return banner

    def _build_xy_panel(self) -> QWidget:
        panel = QGroupBox("XY Position")
        panel.setObjectName("xyPanel")
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 18, 14, 14)

        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.lbl_real_value = self._metric_label("Real\nX -- / Y --")
        self.lbl_target_value = self._metric_label("Target\nX -- / Y --")
        self.lbl_hw_value = self._metric_label("Hardware\nX -- / Y --")
        metrics.addWidget(self.lbl_real_value)
        metrics.addWidget(self.lbl_target_value)
        metrics.addWidget(self.lbl_hw_value)
        layout.addLayout(metrics)

        self.pad = XyPad()
        layout.addWidget(self.pad, 1)
        return panel

    def _build_control_stack(self) -> QWidget:
        tabs = QTabWidget()
        tabs.setObjectName("controlTabs")

        control_page = QWidget()
        control_layout = QVBoxLayout(control_page)
        control_layout.setContentsMargins(0, 10, 0, 0)
        control_layout.setSpacing(12)
        control_layout.addWidget(self._build_output_panel())
        control_layout.addWidget(self._build_nudge_panel())
        control_layout.addStretch(1)

        positioner_page = QWidget()
        positioner_layout = QVBoxLayout(positioner_page)
        positioner_layout.setContentsMargins(0, 10, 0, 0)
        positioner_layout.setSpacing(12)
        positioner_layout.addWidget(self._build_positioner_control_panel())
        positioner_layout.addStretch(1)

        setup_page = QWidget()
        setup_layout = QVBoxLayout(setup_page)
        setup_layout.setContentsMargins(0, 10, 0, 0)
        setup_layout.setSpacing(12)
        setup_layout.addWidget(self._build_mapping_panel())
        setup_layout.addWidget(self._build_positioner_setup_panel())
        setup_layout.addStretch(1)

        tabs.addTab(control_page, "Scanner")
        tabs.addTab(positioner_page, "Positioner")
        tabs.addTab(setup_page, "Setup")
        return tabs

    def _build_output_panel(self) -> QWidget:
        volt_box = QGroupBox("Hardware Output")
        vf = QFormLayout(volt_box)
        vf.setContentsMargins(14, 20, 14, 14)
        vf.setSpacing(10)
        vf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sld_x = QSlider(Qt.Orientation.Horizontal)
        self.sld_y = QSlider(Qt.Orientation.Horizontal)
        self.sld_x.setRange(0, 1000)
        self.sld_y.setRange(0, 1000)
        self.spn_x = QDoubleSpinBox()
        self.spn_y = QDoubleSpinBox()
        for spn in (self.spn_x, self.spn_y):
            spn.setRange(0.0, 10.0)
            spn.setDecimals(3)
            spn.setSingleStep(STEP_PER_MOVE)
            spn.setSuffix(" V")
            spn.setFixedWidth(96)
        vf.addRow(QLabel("X (V)"), self._hbox(self.sld_x, self.spn_x))
        vf.addRow(QLabel("Y (V)"), self._hbox(self.sld_y, self.spn_y))
        return volt_box

    def _build_nudge_panel(self) -> QWidget:
        nudge_box = QGroupBox("Nudge Real-Space")
        ng = QGridLayout(nudge_box)
        ng.setContentsMargins(14, 20, 14, 14)
        ng.setSpacing(8)
        self.lbl_step_value = self._metric_label(f"Step {STEP_PER_MOVE:.2f} V")
        self.btn_left = self._nav_button(QStyle.StandardPixmap.SP_ArrowLeft, "Nudge left")
        self.btn_right = self._nav_button(QStyle.StandardPixmap.SP_ArrowRight, "Nudge right")
        self.btn_up = self._nav_button(QStyle.StandardPixmap.SP_ArrowUp, "Nudge up")
        self.btn_down = self._nav_button(QStyle.StandardPixmap.SP_ArrowDown, "Nudge down")
        ng.addWidget(self.lbl_step_value, 0, 0, 1, 3)
        ng.addWidget(self.btn_up, 1, 1)
        ng.addWidget(self.btn_left, 2, 0)
        ng.addWidget(self.btn_right, 2, 2)
        ng.addWidget(self.btn_down, 3, 1)
        ng.setColumnStretch(0, 1)
        ng.setColumnStretch(1, 1)
        ng.setColumnStretch(2, 1)
        return nudge_box

    def _build_positioner_control_panel(self) -> QWidget:
        box = QGroupBox("ANC300 Positioner")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 20, 14, 14)
        layout.setSpacing(10)

        connection = QHBoxLayout()
        self.lbl_positioner_status = self._chip("Disconnected", "off")
        self.btn_positioner_connect = QPushButton("Connect")
        self.btn_positioner_connect.setProperty("role", "primary")
        connection.addWidget(self.lbl_positioner_status, 1)
        connection.addWidget(self.btn_positioner_connect)
        layout.addLayout(connection)

        self.lbl_positioner_mapping = QLabel("")
        self.lbl_positioner_mapping.setWordWrap(True)
        layout.addWidget(self.lbl_positioner_mapping)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step count"))
        self.spn_positioner_steps = QSpinBox()
        self.spn_positioner_steps.setRange(1, 1000)
        self.spn_positioner_steps.setValue(10)
        step_row.addWidget(self.spn_positioner_steps, 1)
        layout.addLayout(step_row)

        xy = QGridLayout()
        self.btn_pos_left = QPushButton("← Left")
        self.btn_pos_right = QPushButton("Right →")
        self.btn_pos_up = QPushButton("↑ Up")
        self.btn_pos_down = QPushButton("↓ Down")
        xy.addWidget(self.btn_pos_up, 0, 1)
        xy.addWidget(self.btn_pos_left, 1, 0)
        xy.addWidget(self.btn_pos_right, 1, 2)
        xy.addWidget(self.btn_pos_down, 2, 1)
        layout.addLayout(xy)

        z_row = QHBoxLayout()
        self.btn_pos_away = QPushButton("Away from sample")
        self.btn_pos_toward = QPushButton("Toward sample")
        self.btn_pos_toward.setProperty("role", "danger")
        z_row.addWidget(self.btn_pos_away)
        z_row.addWidget(self.btn_pos_toward)
        layout.addLayout(z_row)

        self.btn_positioner_stop = QPushButton("STOP ALL")
        self.btn_positioner_stop.setProperty("role", "danger")
        layout.addWidget(self.btn_positioner_stop)
        return box

    def _build_positioner_setup_panel(self) -> QWidget:
        box = QGroupBox("ANC300 Positioner Setup")
        form = QFormLayout(box)
        form.setContentsMargins(14, 20, 14, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.chk_positioner_enabled = QCheckBox("Enable positioner on this PC")
        self.cmb_positioner_port = QComboBox()
        self.cmb_positioner_port.setEditable(True)
        self.btn_positioner_rescan = QPushButton("Rescan")
        self.btn_positioner_rescan.setProperty("role", "secondary")
        self.cmb_pos_x_axis = QComboBox()
        self.cmb_pos_y_axis = QComboBox()
        self.cmb_pos_z_axis = QComboBox()
        for combo in (self.cmb_pos_x_axis, self.cmb_pos_y_axis, self.cmb_pos_z_axis):
            combo.addItems([str(axis) for axis in range(1, 8)])
        self.cmb_pos_x_positive = QComboBox()
        self.cmb_pos_x_positive.addItem("Left", "left")
        self.cmb_pos_x_positive.addItem("Right", "right")
        self.cmb_pos_y_positive = QComboBox()
        self.cmb_pos_y_positive.addItem("Up", "up")
        self.cmb_pos_y_positive.addItem("Down", "down")
        self.cmb_pos_z_positive = QComboBox()
        self.cmb_pos_z_positive.addItem("Toward sample", "toward")
        self.cmb_pos_z_positive.addItem("Away from sample", "away")
        self.lbl_positioner_setup_state = self._chip("Saved", "ok")
        self.btn_positioner_apply = QPushButton("Apply")
        self.btn_positioner_apply.setProperty("role", "primary")

        form.addRow("Use positioner", self.chk_positioner_enabled)
        form.addRow("COM port", self._hbox(self.cmb_positioner_port, self.btn_positioner_rescan))
        form.addRow("X axis / + direction", self._hbox(self.cmb_pos_x_axis, self.cmb_pos_x_positive))
        form.addRow("Y axis / + direction", self._hbox(self.cmb_pos_y_axis, self.cmb_pos_y_positive))
        form.addRow("Z axis / + direction", self._hbox(self.cmb_pos_z_axis, self.cmb_pos_z_positive))
        form.addRow("State", self.lbl_positioner_setup_state)
        form.addRow("Save", self.btn_positioner_apply)
        return box

    def _build_compact_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("compactRoot")
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(6)

        compact_size = QSize(54, 48)
        self.compact_btn_left = self._nav_button(
            QStyle.StandardPixmap.SP_ArrowLeft, "Nudge left", compact_size
        )
        self.compact_btn_right = self._nav_button(
            QStyle.StandardPixmap.SP_ArrowRight, "Nudge right", compact_size
        )
        self.compact_btn_up = self._nav_button(
            QStyle.StandardPixmap.SP_ArrowUp, "Nudge up", compact_size
        )
        self.compact_btn_down = self._nav_button(
            QStyle.StandardPixmap.SP_ArrowDown, "Nudge down", compact_size
        )
        self.btn_expand = QPushButton()
        self.btn_expand.setObjectName("expandButton")
        self.btn_expand.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton))
        self.btn_expand.setIconSize(QSize(18, 18))
        self.btn_expand.setFixedSize(40, 36)
        self.btn_expand.setToolTip("Return to the full DAQ controller (Esc)")
        self.btn_expand.setAccessibleName("Return to full controller")

        layout.addWidget(self.compact_btn_up, 0, 1, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.compact_btn_left, 1, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.btn_expand, 1, 1, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.compact_btn_right, 1, 2, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.compact_btn_down, 2, 1, Qt.AlignmentFlag.AlignCenter)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(1, 1)
        layout.setRowStretch(2, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)
        return page

    def _build_mapping_panel(self) -> QWidget:
        map_box = QGroupBox("Device / Mapping")
        mf = QFormLayout(map_box)
        mf.setContentsMargins(14, 20, 14, 14)
        mf.setSpacing(10)
        mf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cmb_device = QComboBox()
        self.btn_rescan = QPushButton("Rescan")
        self.btn_rescan.setProperty("role", "secondary")
        self.cmb_x_ch = QComboBox()
        self.cmb_y_ch = QComboBox()
        self.chk_inv_x = QCheckBox("Invert X")
        self.chk_inv_y = QCheckBox("Invert Y")
        self.chk_rot_en = QCheckBox("Rotation")
        self.spn_rot_deg = QDoubleSpinBox()
        self.spn_rot_deg.setRange(-360.0, 360.0)
        self.spn_rot_deg.setDecimals(3)
        self.spn_rot_deg.setSingleStep(1.0)
        self.spn_rot_deg.setSuffix(" deg")
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setProperty("role", "primary")
        self.lbl_mapping_pending = self._chip("Active", "ok")
        self.lbl_mapping_pending.setObjectName("mappingState")
        mf.addRow("Device", self._hbox(self.cmb_device, self.btn_rescan))
        mf.addRow("X Channel", self.cmb_x_ch)
        mf.addRow("Y Channel", self.cmb_y_ch)
        mf.addRow("Inversion", self._hbox(self.chk_inv_x, self.chk_inv_y))
        mf.addRow("Rotation", self._hbox(self.chk_rot_en, self.spn_rot_deg))
        mf.addRow("State", self.lbl_mapping_pending)
        mf.addRow("Apply", self.btn_apply)
        return map_box

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("bottomStatus")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("statusDetail")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status, 1)
        return bar

    def _connect_controls(self) -> None:
        self.chk_enable.toggled.connect(self._on_enable_toggled)
        self.btn_home.clicked.connect(lambda: self._set_target_real(5.0, 5.0))
        self.btn_ground.clicked.connect(self._ground_outputs)
        self.pad.clicked.connect(lambda rx, ry: self._set_target_real(rx, ry))
        self.sld_x.valueChanged.connect(lambda v: self._on_hw_control_changed("x", v / 100.0))
        self.sld_y.valueChanged.connect(lambda v: self._on_hw_control_changed("y", v / 100.0))
        self.spn_x.valueChanged.connect(lambda v: self._on_hw_control_changed("x", float(v)))
        self.spn_y.valueChanged.connect(lambda v: self._on_hw_control_changed("y", float(v)))
        self.btn_left.clicked.connect(lambda: self._nudge_real(-STEP_PER_MOVE, 0.0))
        self.btn_right.clicked.connect(lambda: self._nudge_real(+STEP_PER_MOVE, 0.0))
        self.btn_up.clicked.connect(lambda: self._nudge_real(0.0, +STEP_PER_MOVE))
        self.btn_down.clicked.connect(lambda: self._nudge_real(0.0, -STEP_PER_MOVE))
        self.compact_btn_left.clicked.connect(lambda: self._nudge_real(-STEP_PER_MOVE, 0.0))
        self.compact_btn_right.clicked.connect(lambda: self._nudge_real(+STEP_PER_MOVE, 0.0))
        self.compact_btn_up.clicked.connect(lambda: self._nudge_real(0.0, +STEP_PER_MOVE))
        self.compact_btn_down.clicked.connect(lambda: self._nudge_real(0.0, -STEP_PER_MOVE))
        self.btn_compact.clicked.connect(self._enter_compact_mode)
        self.btn_expand.clicked.connect(self._exit_compact_mode)
        self.btn_about.clicked.connect(self._show_about_dialog)
        self.btn_view_update.clicked.connect(self._open_available_release)
        self.btn_update_later.clicked.connect(lambda: self.update_banner.hide())
        self.btn_skip_update.clicked.connect(self._skip_available_release)
        self.btn_rescan.clicked.connect(self._on_rescan_devices)
        self.cmb_device.currentTextChanged.connect(self._on_device_changed_pending)
        self.cmb_x_ch.currentTextChanged.connect(lambda _: self._update_mapping_dirty())
        self.cmb_y_ch.currentTextChanged.connect(lambda _: self._update_mapping_dirty())
        self.chk_inv_x.toggled.connect(lambda _: self._update_mapping_dirty())
        self.chk_inv_y.toggled.connect(lambda _: self._update_mapping_dirty())
        self.chk_rot_en.toggled.connect(self._on_rotation_enabled_changed)
        self.spn_rot_deg.valueChanged.connect(lambda _: self._update_mapping_dirty())
        self.btn_apply.clicked.connect(self._on_apply_mapping)
        self.btn_positioner_connect.clicked.connect(self._on_positioner_connect_clicked)
        self.btn_positioner_stop.clicked.connect(self._on_positioner_stop_clicked)
        self.btn_pos_left.clicked.connect(lambda: self._request_positioner_move("x", "left"))
        self.btn_pos_right.clicked.connect(lambda: self._request_positioner_move("x", "right"))
        self.btn_pos_up.clicked.connect(lambda: self._request_positioner_move("y", "up"))
        self.btn_pos_down.clicked.connect(lambda: self._request_positioner_move("y", "down"))
        self.btn_pos_toward.clicked.connect(lambda: self._request_positioner_move("z", "toward"))
        self.btn_pos_away.clicked.connect(lambda: self._request_positioner_move("z", "away"))
        self.chk_positioner_enabled.toggled.connect(lambda _: self._update_positioner_setup_dirty())
        self.cmb_positioner_port.currentTextChanged.connect(lambda _: self._update_positioner_setup_dirty())
        for combo in (
            self.cmb_pos_x_axis,
            self.cmb_pos_y_axis,
            self.cmb_pos_z_axis,
            self.cmb_pos_x_positive,
            self.cmb_pos_y_positive,
            self.cmb_pos_z_positive,
        ):
            combo.currentIndexChanged.connect(lambda _: self._update_positioner_setup_dirty())
        self.btn_positioner_rescan.clicked.connect(self._rescan_positioner_ports)
        self.btn_positioner_apply.clicked.connect(self._on_apply_positioner_settings)

    def _automatic_update_checks_enabled(self) -> bool:
        disabled = os.environ.get("DAQ_XY_DISABLE_UPDATE_CHECK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        offscreen = os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen"
        return not disabled and not offscreen

    def _start_automatic_update_check(self) -> None:
        self._start_update_check(manual=False)

    def _start_update_check(self, *, manual: bool) -> None:
        if self._update_thread is not None and self._update_thread.isRunning():
            if manual:
                QMessageBox.information(self, "Check for Updates", "An update check is already in progress.")
            return
        self._update_check_manual = manual
        thread = QThread(self)
        worker = _UpdateWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.check)
        worker.finished.connect(self._on_update_check_finished)
        worker.failed.connect(self._on_update_check_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_update_thread_finished)
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    @pyqtSlot()
    def _on_update_thread_finished(self) -> None:
        thread = self._update_thread
        self._update_thread = None
        self._update_worker = None
        if thread is not None:
            thread.deleteLater()

    def _record_update_check_attempt(self) -> None:
        self._update_preferences.mark_checked()
        try:
            save_update_preferences(_update_prefs_path(), self._update_preferences)
        except Exception:
            LOGGER.debug("Unable to save update-check preferences.", exc_info=True)

    @pyqtSlot(object)
    def _on_update_check_finished(self, release: object) -> None:
        manual = self._update_check_manual
        self._record_update_check_attempt()
        if not isinstance(release, ReleaseInfo):
            self._on_update_check_failed("The update service returned an invalid response.")
            return
        if not is_newer_release(__version__, release.version):
            self._available_release = None
            self.update_banner.hide()
            if manual:
                QMessageBox.information(
                    self,
                    "Check for Updates",
                    f"DAQ XY Control {__version__} is the latest available version.",
                )
            return
        if release.version == self._update_preferences.skipped_version and not manual:
            return
        self._available_release = release
        notes = " ".join(release.notes.split())
        if len(notes) > 220:
            notes = notes[:217].rstrip() + "…"
        message = f"DAQ XY Control {release.version} is available."
        if notes:
            message += f" {notes}"
        self.lbl_update_available.setText(message)
        self.update_banner.show()

    @pyqtSlot(str)
    def _on_update_check_failed(self, message: str) -> None:
        manual = self._update_check_manual
        self._record_update_check_attempt()
        LOGGER.info("Update check unavailable: %s", message)
        if manual:
            QMessageBox.warning(
                self,
                "Check for Updates",
                "Unable to check for updates right now. Hardware control is unaffected.\n\n" + message,
            )

    def _show_about_dialog(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("About DAQ XY Control")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"DAQ XY Control {__version__}")
        box.setInformativeText(
            "Fine scanner control through NI-DAQ with optional ANC300 coarse positioning.\n\n"
            "Update checks do not connect to or command either hardware subsystem."
        )
        check_button = box.addButton("Check for updates", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is check_button:
            self._start_update_check(manual=True)

    def _open_available_release(self) -> None:
        release = self._available_release
        if release is not None:
            QDesktopServices.openUrl(QUrl(release.page_url))

    def _skip_available_release(self) -> None:
        release = self._available_release
        if release is None:
            return
        self._update_preferences.skipped_version = release.version
        try:
            save_update_preferences(_update_prefs_path(), self._update_preferences)
        except Exception:
            LOGGER.debug("Unable to save skipped update version.", exc_info=True)
        self.update_banner.hide()

    def _build_compact_shortcuts(self) -> None:
        shortcut_actions = {
            "up": ("Up", self.compact_btn_up.click),
            "down": ("Down", self.compact_btn_down.click),
            "left": ("Left", self.compact_btn_left.click),
            "right": ("Right", self.compact_btn_right.click),
            "exit": ("Esc", self._exit_compact_mode),
        }
        self._compact_shortcuts: dict[str, QShortcut] = {}
        for name, (key, action) in shortcut_actions.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(action)
            shortcut.setEnabled(False)
            self._compact_shortcuts[name] = shortcut

    def _sync_compact_controls(self) -> None:
        can_nudge = bool(self._enabled and not self._demo_reason)
        for button in (
            self.compact_btn_left,
            self.compact_btn_right,
            self.compact_btn_up,
            self.compact_btn_down,
        ):
            button.setEnabled(can_nudge)
        for name, shortcut in self._compact_shortcuts.items():
            shortcut.setEnabled(bool(self._compact_mode and (name == "exit" or can_nudge)))

    def _update_window_title(self) -> None:
        if not self._compact_mode:
            self.setWindowTitle(self._full_window_title)
            return
        if self._demo_reason:
            state = "DEMO"
        else:
            state = "ON" if self._enabled else "OFF"
        self.setWindowTitle(f"DAQ XY - {state}")

    def _center_on_screen(self, screen: Any | None) -> None:
        """Center this window on ``screen`` without maximizing it."""
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        frame_geometry = self.frameGeometry()
        self.move(_centered_top_left(screen.availableGeometry(), frame_geometry.size()))

    def _center_on_screen_after_frame_update(self, screen: Any | None) -> None:
        """Center now and again after Qt finishes rebuilding the native frame."""
        self._center_on_screen(screen)
        QTimer.singleShot(0, lambda: self._center_on_screen(screen))

    def _enter_compact_mode(self) -> None:
        if self._compact_mode:
            return
        current_screen = self.screen()
        if self.isMaximized() or self.isFullScreen():
            self._full_window_geometry = self.normalGeometry()
        else:
            self._full_window_geometry = self.geometry()
        self._full_window_flags = self.windowFlags()
        self._full_window_minimum_size = self.minimumSize()
        self._full_window_maximum_size = self.maximumSize()
        was_visible = self.isVisible()

        self._compact_mode = True
        self._view_stack.setCurrentWidget(self._compact_page)
        self._sync_compact_controls()
        self._update_window_title()

        _release_windows_native_window_icon(self)
        # Keep this as a primary window so closing it still triggers Qt's normal
        # last-window shutdown; Qt.Tool windows are excluded from that behavior.
        compact_flags = self._full_window_flags | Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(compact_flags)
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.setFixedSize(190, 190)
        if was_visible:
            self.show()
        self._center_on_screen_after_frame_update(current_screen)
        _apply_window_icon(self)

    def _exit_compact_mode(self) -> None:
        if not self._compact_mode:
            return
        current_screen = self.screen()
        was_visible = self.isVisible()
        self._compact_mode = False
        self._view_stack.setCurrentWidget(self._full_page)

        _release_windows_native_window_icon(self)
        if self._full_window_flags is not None:
            self.setWindowFlags(self._full_window_flags)
        if self._full_window_minimum_size is not None:
            self.setMinimumSize(self._full_window_minimum_size)
        if self._full_window_maximum_size is not None:
            self.setMaximumSize(self._full_window_maximum_size)
        self.setWindowState(Qt.WindowState.WindowNoState)
        if self._full_window_geometry is not None:
            self.resize(self._full_window_geometry.size())
        self._sync_compact_controls()
        self._update_window_title()
        if was_visible:
            self.show()
        self._center_on_screen_after_frame_update(current_screen)
        _apply_window_icon(self)

    def _chip(self, text: str, state: str = "neutral") -> QLabel:
        label = QLabel(text)
        label.setObjectName("statusChip")
        label.setProperty("state", state)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(62)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return label

    def _metric_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("metricLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return label

    def _nav_button(
        self, icon: QStyle.StandardPixmap, tooltip: str, size: QSize | None = None
    ) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("navButton")
        btn.setIcon(self.style().standardIcon(icon))
        btn.setIconSize(QSize(22, 22))
        btn.setFixedSize(size or QSize(46, 42))
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
        return btn

    def _apply_modern_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#appRoot {
                background: #f4f7f9;
                color: #17202a;
                font-size: 10pt;
            }
            QWidget#compactRoot {
                background: #f4f7f9;
                color: #17202a;
            }
            QFrame#commandBar, QFrame#bottomStatus {
                background: #ffffff;
                border: 1px solid #d7dee8;
                border-radius: 8px;
            }
            QFrame#updateBanner {
                background: #eff6ff;
                border: 1px solid #93c5fd;
                border-radius: 8px;
            }
            QLabel#appTitle {
                color: #111827;
                font-size: 16pt;
                font-weight: 700;
            }
            QLabel#appSubtitle {
                color: #5b6678;
                font-size: 9pt;
            }
            QGroupBox {
                background: #ffffff;
                border: 1px solid #d7dee8;
                border-radius: 8px;
                margin-top: 14px;
                font-weight: 650;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #243244;
            }
            QTabWidget#controlTabs::pane {
                border: 0;
                top: -1px;
            }
            QTabBar::tab {
                background: #e9eef5;
                border: 1px solid #d7dee8;
                border-bottom-color: #cbd5e1;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 7px 18px;
                margin-right: 4px;
                color: #475569;
                font-weight: 650;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #111827;
                border-bottom-color: #ffffff;
            }
            QLabel#statusChip, QLabel#mappingState, QLabel#metricLabel {
                border: 1px solid #d7dee8;
                border-radius: 6px;
                padding: 4px 7px;
                background: #f8fafc;
                color: #243244;
                font-weight: 600;
            }
            QLabel#statusChip[state="on"] {
                background: #dcfce7;
                border-color: #86efac;
                color: #166534;
            }
            QLabel#statusChip[state="off"] {
                background: #f1f5f9;
                border-color: #cbd5e1;
                color: #475569;
            }
            QLabel#statusChip[state="warning"], QLabel#mappingState[state="dirty"] {
                background: #fef3c7;
                border-color: #fbbf24;
                color: #92400e;
            }
            QLabel#statusChip[state="ok"], QLabel#mappingState[state="ok"] {
                background: #ecfdf5;
                border-color: #86efac;
                color: #166534;
            }
            QLabel#mappingState[state="invalid"] {
                background: #fee2e2;
                border-color: #fca5a5;
                color: #991b1b;
            }
            QLabel#metricLabel {
                min-height: 36px;
                font-weight: 600;
            }
            QLabel#statusDetail {
                color: #334155;
                font-size: 9pt;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 12px;
                color: #17202a;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #f8fafc;
                border-color: #94a3b8;
            }
            QPushButton:pressed {
                background: #e2e8f0;
            }
            QPushButton:disabled {
                background: #f1f5f9;
                color: #94a3b8;
                border-color: #e2e8f0;
            }
            QPushButton[role="primary"] {
                background: #0f766e;
                border-color: #0f766e;
                color: #ffffff;
            }
            QPushButton[role="primary"]:hover {
                background: #115e59;
                border-color: #115e59;
            }
            QPushButton[role="secondary"] {
                background: #f8fafc;
            }
            QPushButton[role="danger"] {
                background: #fee2e2;
                border-color: #fecaca;
                color: #991b1b;
            }
            QPushButton[role="danger"]:hover {
                background: #fecaca;
            }
            QPushButton#navButton {
                padding: 0;
                min-width: 42px;
                min-height: 38px;
            }
            QPushButton#expandButton {
                padding: 0;
                background: #e9eef5;
            }
            QCheckBox {
                spacing: 6px;
                font-weight: 600;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #94a3b8;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #0f766e;
                border-color: #0f766e;
            }
            QSlider::groove:horizontal {
                height: 6px;
                border-radius: 3px;
                background: #dbe5ee;
            }
            QSlider::sub-page:horizontal {
                border-radius: 3px;
                background: #38bdf8;
            }
            QSlider::handle:horizontal {
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
                background: #ffffff;
                border: 2px solid #0284c7;
            }
            QComboBox, QDoubleSpinBox, QSpinBox {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 5px 7px;
                min-height: 24px;
            }
            QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
                border-color: #0284c7;
            }
            """
        )

    def _hbox(self, *widgets: QWidget) -> QWidget:
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        for it in widgets:
            l.addWidget(it)
        return w

    def _set_widget_state(self, widget: QWidget, state: str) -> None:
        widget.setProperty("state", state)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_chip(self, label: QLabel, text: str, state: str) -> None:
        label.setText(text)
        self._set_widget_state(label, state)

    def _on_rotation_enabled_changed(self, checked: bool) -> None:
        self.spn_rot_deg.setEnabled(bool(checked))
        self._update_mapping_dirty()

    def _update_mapping_dirty(self) -> None:
        if not hasattr(self, "btn_apply"):
            return
        dev = self.cmb_device.currentText().strip()
        chx = self.cmb_x_ch.currentText().strip()
        chy = self.cmb_y_ch.currentText().strip()
        rotation_enabled = self.chk_rot_en.isChecked()
        rotation_deg = float(self.spn_rot_deg.value()) if rotation_enabled else 0.0
        dirty = any(
            (
                dev != self._selected_device,
                chx != self._ao_x,
                chy != self._ao_y,
                self.chk_inv_x.isChecked() != self._mapping.invert_x,
                self.chk_inv_y.isChecked() != self._mapping.invert_y,
                rotation_enabled != self._mapping.rotation_enabled,
                abs(rotation_deg - self._mapping.rotation_deg) > 1e-9,
            )
        )
        valid = bool(chx and chy and chx != chy)
        self._mapping_dirty = dirty
        self.btn_apply.setEnabled(bool(dirty and valid))
        if not valid:
            self._set_chip(self.lbl_mapping_pending, "Invalid", "invalid")
        elif dirty:
            self._set_chip(self.lbl_mapping_pending, "Pending", "dirty")
        else:
            self._set_chip(self.lbl_mapping_pending, "Active", "ok")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.chk_enable.setEnabled(enabled)
        self.btn_home.setEnabled(enabled)
        self.btn_ground.setEnabled(enabled)
        self.pad.setEnabled(enabled)
        self.sld_x.setEnabled(enabled)
        self.sld_y.setEnabled(enabled)
        self.spn_x.setEnabled(enabled)
        self.spn_y.setEnabled(enabled)
        self.btn_left.setEnabled(enabled)
        self.btn_right.setEnabled(enabled)
        self.btn_up.setEnabled(enabled)
        self.btn_down.setEnabled(enabled)
        self._sync_compact_controls()

    def _populate_mapping_controls(self) -> None:
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self.cmb_device.addItems(self._devices)
        if self._selected_device:
            self.cmb_device.setCurrentText(self._selected_device)
        self.cmb_device.blockSignals(False)
        self._refresh_channel_dropdowns(self.cmb_device.currentText())
        self.chk_inv_x.setChecked(self._mapping.invert_x)
        self.chk_inv_y.setChecked(self._mapping.invert_y)
        self.chk_rot_en.setChecked(self._mapping.rotation_enabled)
        self.spn_rot_deg.setValue(self._mapping.rotation_deg)
        self.spn_rot_deg.setEnabled(self.chk_rot_en.isChecked())
        self.pad.set_voltage_range(self._vmin, self._vmax, self._vmin, self._vmax)
        self._update_pad_projection()
        self._update_mapping_dirty()

    def _populate_positioner_controls(self) -> None:
        settings = self._positioner_settings
        widgets = (
            self.chk_positioner_enabled,
            self.cmb_positioner_port,
            self.cmb_pos_x_axis,
            self.cmb_pos_y_axis,
            self.cmb_pos_z_axis,
            self.cmb_pos_x_positive,
            self.cmb_pos_y_positive,
            self.cmb_pos_z_positive,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.chk_positioner_enabled.setChecked(settings.enabled)
        self._rescan_positioner_ports(update_state=False)
        if settings.port:
            if self.cmb_positioner_port.findText(settings.port) < 0:
                self.cmb_positioner_port.addItem(settings.port)
            self.cmb_positioner_port.setCurrentText(settings.port)
        self.cmb_pos_x_axis.setCurrentText(str(settings.x_axis))
        self.cmb_pos_y_axis.setCurrentText(str(settings.y_axis))
        self.cmb_pos_z_axis.setCurrentText(str(settings.z_axis))
        self.cmb_pos_x_positive.setCurrentIndex(self.cmb_pos_x_positive.findData(settings.x_positive))
        self.cmb_pos_y_positive.setCurrentIndex(self.cmb_pos_y_positive.findData(settings.y_positive))
        self.cmb_pos_z_positive.setCurrentIndex(self.cmb_pos_z_positive.findData(settings.z_positive))
        for widget in widgets:
            widget.blockSignals(False)
        self._update_positioner_setup_dirty()
        self._update_positioner_controls()

    def _positioner_settings_from_controls(self) -> PositionerSettings:
        return PositionerSettings(
            enabled=self.chk_positioner_enabled.isChecked(),
            port=self.cmb_positioner_port.currentText().strip(),
            baudrate=38400,
            x_axis=int(self.cmb_pos_x_axis.currentText()),
            y_axis=int(self.cmb_pos_y_axis.currentText()),
            z_axis=int(self.cmb_pos_z_axis.currentText()),
            x_positive=str(self.cmb_pos_x_positive.currentData()),
            y_positive=str(self.cmb_pos_y_positive.currentData()),
            z_positive=str(self.cmb_pos_z_positive.currentData()),
        )

    def _update_positioner_setup_dirty(self) -> None:
        if not hasattr(self, "btn_positioner_apply"):
            return
        candidate = self._positioner_settings_from_controls()
        dirty = candidate != self._positioner_settings
        try:
            candidate.validate()
            valid = True
        except ValueError:
            valid = False
        self.btn_positioner_apply.setEnabled(dirty and valid)
        if not valid:
            self._set_chip(self.lbl_positioner_setup_state, "Invalid", "warning")
        elif dirty:
            self._set_chip(self.lbl_positioner_setup_state, "Pending", "warning")
        else:
            self._set_chip(self.lbl_positioner_setup_state, "Saved", "ok")

    def _rescan_positioner_ports(self, _checked: bool = False, update_state: bool = True) -> None:
        current = self.cmb_positioner_port.currentText().strip()
        ports = list_serial_ports()
        self.cmb_positioner_port.blockSignals(True)
        self.cmb_positioner_port.clear()
        self.cmb_positioner_port.addItems(ports)
        if current:
            if self.cmb_positioner_port.findText(current) < 0:
                self.cmb_positioner_port.addItem(current)
            self.cmb_positioner_port.setCurrentText(current)
        self.cmb_positioner_port.blockSignals(False)
        if update_state:
            self._update_positioner_setup_dirty()

    def _on_apply_positioner_settings(self) -> None:
        candidate = self._positioner_settings_from_controls()
        try:
            candidate.validate()
            save_positioner_settings(_positioner_prefs_path(), candidate)
        except Exception as exc:
            QMessageBox.warning(self, "Positioner Setup", f"Unable to save positioner settings: {exc}")
            return
        if self._positioner_connected and candidate != self._positioner_settings:
            self._positioner_busy = True
            self._positioner_disconnect_requested.emit()
            self._positioner_connected = False
        self._positioner_settings = candidate
        self._update_positioner_setup_dirty()
        self._update_positioner_controls("Settings saved. Connect explicitly when ready.")

    def _update_positioner_controls(self, detail: str = "") -> None:
        if not hasattr(self, "btn_positioner_connect"):
            return
        settings = self._positioner_settings
        enabled = settings.enabled
        motion_enabled = enabled and self._positioner_connected and not self._positioner_busy
        for button in (
            self.btn_pos_left,
            self.btn_pos_right,
            self.btn_pos_up,
            self.btn_pos_down,
            self.btn_pos_toward,
            self.btn_pos_away,
        ):
            button.setEnabled(motion_enabled)
        self.spn_positioner_steps.setEnabled(motion_enabled)
        self.btn_positioner_stop.setEnabled(enabled and self._positioner_connected)
        self.btn_positioner_connect.setEnabled(enabled and not self._positioner_busy)
        self.btn_positioner_connect.setText("Disconnect" if self._positioner_connected else "Connect")
        if not enabled:
            self._set_chip(self.lbl_positioner_status, "Not configured", "off")
        elif self._positioner_connected:
            self._set_chip(
                self.lbl_positioner_status,
                "Busy" if self._positioner_busy else "Connected",
                "warning" if self._positioner_busy else "ok",
            )
        else:
            self._set_chip(self.lbl_positioner_status, "Disconnected", "off")
        self.lbl_positioner_mapping.setText(
            f"{settings.port or 'No COM port'} | "
            f"X={settings.x_axis} (+ is {settings.x_positive}) | "
            f"Y={settings.y_axis} (+ is {settings.y_positive}) | "
            f"Z={settings.z_axis} (+ is {settings.z_positive} sample)"
            + (f"\n{detail}" if detail else "")
        )

    def _on_positioner_connect_clicked(self) -> None:
        if self._positioner_connected:
            self._positioner_busy = True
            self._update_positioner_controls("Disconnecting…")
            self._positioner_disconnect_requested.emit()
            return
        try:
            self._positioner_settings.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Positioner", str(exc))
            return
        self._positioner_busy = True
        self._update_positioner_controls("Connecting without sending movement commands…")
        self._positioner_connect_requested.emit(self._positioner_settings)

    def _request_positioner_move(self, axis: str, direction: str) -> None:
        if not self._positioner_connected or self._positioner_busy:
            return
        steps = int(self.spn_positioner_steps.value())
        if axis == "z" and steps > 100:
            QMessageBox.warning(self, "Positioner Z Limit", "Z moves are limited to 100 steps per command.")
            return
        self._positioner_busy = True
        self._update_positioner_controls(f"Moving {direction}…")
        self._positioner_move_requested.emit(self._positioner_settings, axis, direction, steps)

    def _on_positioner_stop_clicked(self) -> None:
        if self._positioner_connected:
            self._positioner_busy = True
            self._update_positioner_controls("Stopping…")
            self._positioner_stop_requested.emit()

    @pyqtSlot(str)
    def _on_positioner_connected(self, version: str) -> None:
        self._positioner_connected = True
        self._positioner_busy = False
        self._positioner_version = version
        self._update_positioner_controls("ANC300 identity and stepping modes verified.")

    @pyqtSlot(str)
    def _on_positioner_disconnected(self, reason: str) -> None:
        self._positioner_connected = False
        self._positioner_busy = False
        self._positioner_version = ""
        self._update_positioner_controls(reason)

    @pyqtSlot(str)
    def _on_positioner_operation_started(self, operation: str) -> None:
        self._positioner_busy = True
        self._update_positioner_controls(operation + "…")

    @pyqtSlot(str)
    def _on_positioner_operation_finished(self, detail: str) -> None:
        self._positioner_busy = False
        self._update_positioner_controls(detail)

    @pyqtSlot(str)
    def _on_positioner_failed(self, message: str) -> None:
        self._positioner_connected = False
        self._positioner_busy = False
        self._update_positioner_controls(f"Error: {message}")
        if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen":
            QMessageBox.warning(self, "ANC300 Positioner", message)

    def _update_pad_projection(self) -> None:
        poly = reachable_real_polygon(self._mapping)
        xmin, xmax, ymin, ymax = reachable_real_bounds(self._mapping, margin_ratio=0.08)
        self.pad.set_boundary_polygon(poly)
        self.pad.set_view_bounds(xmin, xmax, ymin, ymax)

    def _refresh_channel_dropdowns(self, dev_name: str) -> None:
        chans = self._channels_by_device.get(dev_name, [])
        self.cmb_x_ch.clear()
        self.cmb_y_ch.clear()
        self.cmb_x_ch.addItems(chans)
        self.cmb_y_ch.addItems(chans)
        if self._ao_x in chans:
            self.cmb_x_ch.setCurrentText(self._ao_x)
        elif chans:
            self.cmb_x_ch.setCurrentIndex(0)
        if self._ao_y in chans:
            self.cmb_y_ch.setCurrentText(self._ao_y)
        elif len(chans) > 1:
            self.cmb_y_ch.setCurrentIndex(1)
        elif chans:
            self.cmb_y_ch.setCurrentIndex(0)

    def _apply_demo_mode_if_needed(self) -> None:
        if not self._demo_reason:
            return
        self._set_controls_enabled(False)
        self.chk_enable.setChecked(False)
        if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen":
            QMessageBox.warning(self, "No Hardware / Demo Mode", self._demo_reason)

    def _sync_ui(self) -> None:
        self._syncing = True
        self.pad.set_real_xy(self._rx, self._ry)
        self.pad.set_target_real_xy(self._target_rx, self._target_ry)
        self.pad.set_rotation(self._mapping.rotation_enabled, self._mapping.rotation_deg)
        self.sld_x.blockSignals(True)
        self.sld_y.blockSignals(True)
        self.spn_x.blockSignals(True)
        self.spn_y.blockSignals(True)
        self.sld_x.setValue(int(round(self._vx * 100.0)))
        self.sld_y.setValue(int(round(self._vy * 100.0)))
        self.spn_x.setValue(self._vx)
        self.spn_y.setValue(self._vy)
        self.sld_x.blockSignals(False)
        self.sld_y.blockSignals(False)
        self.spn_x.blockSignals(False)
        self.spn_y.blockSignals(False)
        self._syncing = False
        self._update_live_values()
        self._update_status()

    def _update_live_values(self) -> None:
        if not hasattr(self, "lbl_real_value"):
            return
        self.lbl_real_value.setText(f"Real\nX {self._rx:.3f} / Y {self._ry:.3f}")
        self.lbl_target_value.setText(f"Target\nX {self._target_rx:.3f} / Y {self._target_ry:.3f}")
        self.lbl_hw_value.setText(f"Hardware\nX {self._vx:.3f} V / Y {self._vy:.3f} V")

    def _update_status(self) -> None:
        self._sync_compact_controls()
        self._update_window_title()
        if self._demo_reason:
            if hasattr(self, "lbl_output_chip"):
                self._set_chip(self.lbl_output_chip, "OFF", "off")
                self._set_chip(self.lbl_readback_chip, "Demo", "warning")
                self._set_chip(self.lbl_device_chip, "--", "neutral")
            self.lbl_status.setText(f"Demo mode: {self._demo_reason}")
            return
        en = "ON" if self._enabled else "OFF"
        readback_state = "cached/uncertain" if self._readback_uncertain else "measured"
        if hasattr(self, "lbl_output_chip"):
            self._set_chip(self.lbl_output_chip, en, "on" if self._enabled else "off")
            self._set_chip(
                self.lbl_readback_chip,
                "Uncertain" if self._readback_uncertain else "Measured",
                "warning" if self._readback_uncertain else "ok",
            )
            self._set_chip(self.lbl_device_chip, self._selected_device or "--", "neutral")
        self.lbl_status.setToolTip(self._readback_status)
        self.lbl_status.setText(
            f"Output {en} | V_hw=({self._vx:.3f}, {self._vy:.3f}) V | "
            f"R=({self._rx:.3f}, {self._ry:.3f}) | "
            f"readback={readback_state} | "
            f"invert=({int(self._mapping.invert_x)},{int(self._mapping.invert_y)}) "
            f"rot={'on' if self._mapping.rotation_enabled else 'off'}:{self._mapping.rotation_deg:.1f}deg "
            f"ch=({self._ao_x},{self._ao_y}) dev={self._selected_device} "
            f"range=[{self._vmin:.1f},{self._vmax:.1f}]"
        )

    def _pull_outputs_from_daq(self) -> None:
        self._vx, self._vy = self._daq.read_outputs()
        self._readback_uncertain = bool(getattr(self._daq, "readback_uncertain", False))
        self._readback_status = str(
            getattr(self._daq, "readback_status", "Hardware AO readback status is unavailable.")
        )

    def _freeze_targets_at_current_output(self) -> None:
        self._target_vx = self._vx
        self._target_vy = self._vy
        self._target_rx = self._rx
        self._target_ry = self._ry

    def _refresh_from_hardware(self) -> None:
        self._pull_outputs_from_daq()
        self._rx, self._ry = map_hw_to_real(self._vx, self._vy, self._mapping)
        self._freeze_targets_at_current_output()

    def _set_target_hw(self, vx: float, vy: float) -> None:
        self._target_vx = clamp_voltage(vx)
        self._target_vy = clamp_voltage(vy)
        self._target_rx, self._target_ry = map_hw_to_real(self._target_vx, self._target_vy, self._mapping)
        if self._enabled:
            self._start_ramp()

    def _set_target_real(self, rx: float, ry: float) -> None:
        # Project requested real-space point onto the true reachable region.
        trg_vx, trg_vy = map_real_to_hw(float(rx), float(ry), self._mapping)
        trg_rx, trg_ry = map_hw_to_real(trg_vx, trg_vy, self._mapping)
        self._target_rx = float(trg_rx)
        self._target_ry = float(trg_ry)
        self._set_target_hw(trg_vx, trg_vy)

    def _nudge_real(self, drx: float, dry: float) -> None:
        base_rx = self._target_rx
        base_ry = self._target_ry
        next_rx = base_rx + drx
        next_ry = base_ry + dry
        next_vx, next_vy = map_real_to_hw(next_rx, next_ry, self._mapping)
        if self._debug_mapping:
            LOGGER.info(
                "NUDGE dR=(%.3f,%.3f) targetR=(%.3f,%.3f) -> targetV=(%.3f,%.3f) invert=(%s,%s) rot=%s %.2f",
                drx,
                dry,
                next_rx,
                next_ry,
                next_vx,
                next_vy,
                self._mapping.invert_x,
                self._mapping.invert_y,
                self._mapping.rotation_enabled,
                self._mapping.rotation_deg,
            )
        self._set_target_real(next_rx, next_ry)

    def _on_enable_toggled(self, checked: bool) -> None:
        if self._demo_reason:
            self.chk_enable.setChecked(False)
            self._enabled = False
            self._update_status()
            return
        self._enabled = bool(checked)
        if not self._enabled:
            self._ramp_timer.stop()
            self._freeze_targets_at_current_output()
        self._update_status()

    def _on_hw_control_changed(self, axis: str, value: float) -> None:
        if self._syncing:
            return
        if axis == "x":
            self._set_target_hw(value, self._target_vy)
        else:
            self._set_target_hw(self._target_vx, value)
        # Hardware widgets are output readbacks, so immediately re-sync to actual values.
        self._sync_ui()

    def _ground_outputs(self) -> None:
        # Ground is explicit hardware operation: bypass real-space mapping.
        if not self._enabled:
            self.chk_enable.setChecked(True)
        self._set_target_hw(0.0, 0.0)

    def _start_ramp(self) -> None:
        if self.ramp.dwell_ms <= 0:
            self.ramp.dwell_ms = 50
        if not self._ramp_timer.isActive():
            self._ramp_timer.start(self.ramp.dwell_ms)

    def _ramp_step(self) -> None:
        if not self._enabled:
            self._ramp_timer.stop()
            return
        dx = self._target_vx - self._vx
        dy = self._target_vy - self._vy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            self._ramp_timer.stop()
            return
        nx, ny = _next_ramp_point(self._vx, self._vy, self._target_vx, self._target_vy, self.ramp.step_v)
        try:
            self._vx, self._vy = self._daq.write_outputs(nx, ny)
            self._readback_uncertain = bool(getattr(self._daq, "readback_uncertain", False))
            self._readback_status = str(
                getattr(self._daq, "readback_status", "Hardware AO readback status is unavailable.")
            )
            self._rx, self._ry = map_hw_to_real(self._vx, self._vy, self._mapping)
        except Exception:
            self._ramp_timer.stop()
            self._enabled = False
            self.chk_enable.blockSignals(True)
            self.chk_enable.setChecked(False)
            self.chk_enable.blockSignals(False)
            self._freeze_targets_at_current_output()
            LOGGER.exception("Scanner control lost during ramp; preserved existing AO outputs and stopped further writes.")
            if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen":
                QMessageBox.warning(
                    self,
                    "Scanner Control Lost",
                    "A DAQ write/read error occurred. The UI stopped ramping and did not force the scanner outputs to 0 V.",
                )
            self._sync_ui()
            return
        if self._debug_mapping:
            LOGGER.info(
                "WRITE V_hw=(%.4f, %.4f) -> R=(%.4f, %.4f) invert=(%s,%s) rot=%s %.2f",
                self._vx,
                self._vy,
                self._rx,
                self._ry,
                self._mapping.invert_x,
                self._mapping.invert_y,
                self._mapping.rotation_enabled,
                self._mapping.rotation_deg,
            )
        self._sync_ui()

    def _on_rescan_devices(self) -> None:
        devices, channels_by_device, err = _detect_devices_and_channels()
        self._devices = devices
        self._channels_by_device = channels_by_device
        self._populate_mapping_controls()
        if err:
            QMessageBox.warning(self, "Rescan", err)
        elif not devices:
            QMessageBox.warning(self, "Rescan", "No NI-DAQ devices detected.")

    def _on_device_changed_pending(self, dev_name: str) -> None:
        self._refresh_channel_dropdowns(dev_name)
        self._update_mapping_dirty()

    def _on_apply_mapping(self) -> None:
        dev = self.cmb_device.currentText().strip()
        chx = self.cmb_x_ch.currentText().strip()
        chy = self.cmb_y_ch.currentText().strip()
        if not chx or not chy:
            QMessageBox.warning(self, "Apply Mapping", "Selected device has no AO channels.")
            return
        if chx == chy:
            QMessageBox.warning(self, "Apply Mapping", "X and Y channels must be different.")
            return

        new_mapping = MappingSettings(
            invert_x=self.chk_inv_x.isChecked(),
            invert_y=self.chk_inv_y.isChecked(),
            rotation_enabled=self.chk_rot_en.isChecked(),
            rotation_deg=float(self.spn_rot_deg.value()) if self.chk_rot_en.isChecked() else 0.0,
        )

        _ramp_was_active = self._ramp_timer.isActive()
        daq_connection_swapped = False
        try:
            device_or_channel_changed = bool(
                (not self._demo_reason) and (dev != self._selected_device or chx != self._ao_x or chy != self._ao_y)
            )
            if not self._demo_reason and (dev != self._selected_device or chx != self._ao_x or chy != self._ao_y):
                if _ramp_was_active:
                    LOGGER.info(
                        "Stopping active ramp before reconnecting DAQ channels so scanner outputs remain undisturbed."
                    )
                    self._ramp_timer.stop()
                new_daq = DaqInterface(dev, chx, chy)
                old_daq = self._daq
                self._daq = new_daq
                daq_connection_swapped = True
                self._selected_device = dev
                self._ao_x = chx
                self._ao_y = chy
                try:
                    old_daq.close()
                except Exception:
                    LOGGER.debug("Failed closing previous DAQ interface.", exc_info=True)

            # Mapping changes do not command movement; they only change interpretation.
            self._mapping = new_mapping
            self._update_pad_projection()
            if device_or_channel_changed:
                self._refresh_from_hardware()
            else:
                # Keep hardware setpoints unchanged; only remap display coordinates.
                self._rx, self._ry = map_hw_to_real(self._vx, self._vy, self._mapping)
                self._target_vx = self._vx
                self._target_vy = self._vy
                self._target_rx = self._rx
                self._target_ry = self._ry
            self._sync_ui()
        except Exception as exc:
            # If the apply failed after stopping an in-flight ramp, restart the ramp
            # only when we are still on the original DAQ connection.
            if (
                _ramp_was_active
                and self._enabled
                and not self._ramp_timer.isActive()
                and not daq_connection_swapped
            ):
                LOGGER.warning(
                    "Apply Mapping failed; restarting interrupted ramp towards previous target "
                    "using existing DAQ connection. Error: %s", exc
                )
                self._start_ramp()
            self._sync_ui()
            QMessageBox.warning(self, "Apply Mapping", str(exc))
            return

        try:
            _save_persisted_mapping(
                PersistedMapping(
                    selected_device_name=self._selected_device,
                    x_channel=self._ao_x,
                    y_channel=self._ao_y,
                    invert_x=self._mapping.invert_x,
                    invert_y=self._mapping.invert_y,
                    rotation_enabled=self._mapping.rotation_enabled,
                    rotation_deg=self._mapping.rotation_deg,
                )
            )
        except Exception as exc:
            LOGGER.warning("Applied mapping successfully, but failed to save persisted settings: %s", exc, exc_info=True)
            if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen":
                QMessageBox.warning(
                    self,
                    "Apply Mapping",
                    f"Mapping was applied, but settings could not be saved: {exc}",
                )
        self._update_mapping_dirty()

    def closeEvent(self, e: Any) -> None:  # type: ignore[override]
        try:
            self._ramp_timer.stop()
            if self._update_thread is not None and self._update_thread.isRunning():
                self._update_thread.quit()
                if not self._update_thread.wait(6000):
                    LOGGER.warning("Update-check worker did not stop before the UI closed.")
            if hasattr(self, "_positioner_thread") and self._positioner_thread.isRunning():
                self._positioner_shutdown_requested.emit()
                if not self._positioner_thread.wait(8000):
                    LOGGER.warning("Positioner worker did not stop before the UI closed.")
            LOGGER.info("Closing scanner UI without altering current AO outputs.")
            self._daq.close()
        finally:
            _release_windows_native_window_icon(self)
            super().closeEvent(e)


def _ensure_qt_in_notebook() -> None:
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            ip.run_line_magic("gui", "qt")
    except Exception:
        pass


def _auto_window(dev_name: str = "Dev1", ao_x: str = "ao0", ao_y: str = "ao1") -> DaqXYWindow:
    persisted = _load_persisted_mapping()
    devices, channels_by_device, enum_err = _detect_devices_and_channels()

    selected_dev = dev_name if dev_name in devices else persisted.selected_device_name
    if selected_dev not in devices and devices:
        selected_dev = devices[0]

    if selected_dev:
        chans = channels_by_device.get(selected_dev, [])
    else:
        chans = []
    selected_x = ao_x if ao_x in chans else persisted.x_channel
    selected_y = ao_y if ao_y in chans else persisted.y_channel
    if selected_x not in chans and chans:
        selected_x = chans[0]
    if selected_y not in chans:
        selected_y = chans[1] if len(chans) > 1 else (chans[0] if chans else selected_y)
    if selected_x == selected_y and len(chans) > 1:
        selected_y = chans[1]

    mapping = persisted.to_mapping_settings()

    if _RealDaqControl is None:
        reason = (
            "Real DAQ backend unavailable. Install iv_automation.py dependencies "
            "(nidaqmx, pyvisa, numpy)."
        )
        return DaqXYWindow(
            dev_name=selected_dev or dev_name,
            ao_x=selected_x or ao_x,
            ao_y=selected_y or ao_y,
            mapping=mapping,
            devices=devices,
            channels_by_device=channels_by_device,
            demo_reason=reason,
        )

    if not devices:
        reason = "No NI-DAQ devices detected."
        if enum_err:
            reason += f" ({enum_err})"
        return DaqXYWindow(
            dev_name=dev_name,
            ao_x=ao_x,
            ao_y=ao_y,
            mapping=mapping,
            devices=devices,
            channels_by_device=channels_by_device,
            demo_reason=reason,
        )

    try:
        return DaqXYWindow(
            dev_name=selected_dev,
            ao_x=selected_x,
            ao_y=selected_y,
            mapping=mapping,
            devices=devices,
            channels_by_device=channels_by_device,
            demo_reason=None,
        )
    except Exception as exc:
        # Device was detected but connection failed (e.g. USB hot-unplug between
        # enumeration and the constructor). Fall back to demo mode so the process
        # does not crash.
        reason = f"DAQ connection failed for {selected_dev} ({selected_x},{selected_y}): {exc}"
        LOGGER.error("Real DAQ startup failed; falling back to demo mode. %s", reason, exc_info=True)
        return DaqXYWindow(
            dev_name=selected_dev,
            ao_x=selected_x,
            ao_y=selected_y,
            mapping=mapping,
            devices=devices,
            channels_by_device=channels_by_device,
            demo_reason=reason,
        )


def launch(dev_name: str = "Dev1", ao_x: str = "ao0", ao_y: str = "ao1") -> tuple[QApplication, DaqXYWindow]:
    app = _qt_application()
    _ensure_qt_in_notebook()
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    win.resize(1180, 720)
    win.show()
    _apply_window_icon(win)
    return app, win


def run(
    dev_name: str = "Dev1",
    ao_x: str = "ao0",
    ao_y: str = "ao1",
    *,
    return_app_and_window: bool = False,
    block: bool = True,
) -> tuple[QApplication, DaqXYWindow] | None:
    app = _qt_application()
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    win.resize(1180, 720)
    win.show()
    _apply_window_icon(win)
    if return_app_and_window:
        return app, win
    if not block:
        _ensure_qt_in_notebook()
        return app, win
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
