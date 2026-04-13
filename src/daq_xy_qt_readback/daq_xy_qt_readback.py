"""PyQt6 NI-DAQ XY control with explicit real-space vs hardware mapping."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
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
V_MIN_DEFAULT = 0.0
V_MAX_DEFAULT = 10.0

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
        self.setMinimumSize(240, 240)
        self._rx = 0.0
        self._ry = 0.0
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
        pen = QPen()
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(int(x0), int(y0), int(side - 1.0), int(side - 1.0))
        for i in range(1, 5):
            xi = x0 + i * (side / 5.0)
            yi = y0 + i * (side / 5.0)
            p.drawLine(int(xi), int(y0), int(xi), int(y1))
            p.drawLine(int(x0), int(yi), int(x1), int(yi))

        # Reachable region boundary in real-space (mapped from hardware square).
        if len(self._boundary_real) >= 2:
            for i in range(len(self._boundary_real)):
                ax, ay = self._boundary_real[i]
                bx, by = self._boundary_real[(i + 1) % len(self._boundary_real)]
                p1x, p1y = self._to_plot(ax, ay)
                p2x, p2y = self._to_plot(bx, by)
                p.drawLine(int(p1x), int(p1y), int(p2x), int(p2y))

        # Fixed display axes in real-space frame.
        cxp, cyp = self._to_plot((self._view_xmin + self._view_xmax) * 0.5, (self._view_ymin + self._view_ymax) * 0.5)
        axis_len = side * 0.22
        x_tip = (cxp + axis_len, cyp)
        y_tip = (cxp, cyp - axis_len)
        p.drawLine(int(cxp), int(cyp), int(x_tip[0]), int(x_tip[1]))
        p.drawLine(int(cxp), int(cyp), int(y_tip[0]), int(y_tip[1]))
        p.drawLine(int(x_tip[0]), int(x_tip[1]), int(x_tip[0] - 8), int(x_tip[1] - 4))
        p.drawLine(int(x_tip[0]), int(x_tip[1]), int(x_tip[0] - 8), int(x_tip[1] + 4))
        p.drawLine(int(y_tip[0]), int(y_tip[1]), int(y_tip[0] - 4), int(y_tip[1] + 8))
        p.drawLine(int(y_tip[0]), int(y_tip[1]), int(y_tip[0] + 4), int(y_tip[1] + 8))
        p.drawText(int(x_tip[0] + 6), int(x_tip[1] + 4), "X")
        p.drawText(int(y_tip[0] + 6), int(y_tip[1] + 4), "Y")

        # Cursor (real-space position)
        px, py = self._to_plot(self._rx, self._ry)
        p.drawLine(int(px - 6), int(py), int(px + 6), int(py))
        p.drawLine(int(px), int(py - 6), int(px), int(py + 6))

        # Range labels.
        p.drawText(int(x0) - 4, int(y1) + 16, f"{self._view_xmin:.2f}")
        p.drawText(int(x1) - 44, int(y1) + 16, f"{self._view_xmax:.2f}")
        p.drawText(int(x0) - 44, int(y1), f"{self._view_ymin:.2f}")
        p.drawText(int(x0) - 44, int(y0) + 10, f"{self._view_ymax:.2f}")


@dataclass
class RampConfig:
    step_v: float = STEP_PER_MOVE
    dwell_ms: int = 100


class DaqXYWindow(QMainWindow):
    """Main UI keeping hardware outputs and real-space display separated."""

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
        self.setWindowTitle(f"NI-DAQ XY Control - Ramped {STEP_PER_MOVE}V @ 100ms")
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
        self._readback_uncertain = False
        self._readback_status = ""
        self._vmin = V_MIN_DEFAULT
        self._vmax = V_MAX_DEFAULT

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
        self._sync_ui()
        self._apply_demo_mode_if_needed()

    def _build_ui(self) -> None:
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)

        top = QHBoxLayout()
        self.chk_enable = QCheckBox("Enable Output")
        self.btn_home = QPushButton("Home (real 5.0,5.0)")
        self.btn_ground = QPushButton("Ground (ramp outputs to 0V)")
        top.addWidget(self.chk_enable)
        top.addWidget(self.btn_home)
        top.addWidget(self.btn_ground)
        top.addStretch(1)
        root.addLayout(top)

        center = QHBoxLayout()
        self.pad = XyPad()
        center.addWidget(self.pad, 2)

        volt_box = QGroupBox("Hardware Outputs")
        vf = QFormLayout(volt_box)
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
        vf.addRow(QLabel("X (V)"), self._hbox(self.sld_x, self.spn_x))
        vf.addRow(QLabel("Y (V)"), self._hbox(self.sld_y, self.spn_y))
        center.addWidget(volt_box, 3)

        nudge_box = QGroupBox(f"Nudge Real-Space (+/-{STEP_PER_MOVE})")
        ng = QGridLayout(nudge_box)
        self.btn_left = QPushButton("Left")
        self.btn_right = QPushButton("Right")
        self.btn_up = QPushButton("Up")
        self.btn_down = QPushButton("Down")
        ng.addWidget(self.btn_up, 0, 1)
        ng.addWidget(self.btn_left, 1, 0)
        ng.addWidget(self.btn_right, 1, 2)
        ng.addWidget(self.btn_down, 2, 1)
        center.addWidget(nudge_box, 2)
        root.addLayout(center)

        map_box = QGroupBox("Device / Mapping")
        mf = QFormLayout(map_box)
        self.cmb_device = QComboBox()
        self.btn_rescan = QPushButton("Rescan")
        self.cmb_x_ch = QComboBox()
        self.cmb_y_ch = QComboBox()
        self.chk_inv_x = QCheckBox("Invert X")
        self.chk_inv_y = QCheckBox("Invert Y")
        self.chk_rot_en = QCheckBox("Enable Rotation")
        self.spn_rot_deg = QDoubleSpinBox()
        self.spn_rot_deg.setRange(-360.0, 360.0)
        self.spn_rot_deg.setDecimals(3)
        self.spn_rot_deg.setSingleStep(1.0)
        self.btn_apply = QPushButton("Apply")
        mf.addRow("Device", self._hbox(self.cmb_device, self.btn_rescan))
        mf.addRow("X Channel", self.cmb_x_ch)
        mf.addRow("Y Channel", self.cmb_y_ch)
        mf.addRow("Inversion", self._hbox(self.chk_inv_x, self.chk_inv_y))
        mf.addRow("Rotation", self._hbox(self.chk_rot_en, self.spn_rot_deg))
        mf.addRow("Apply", self.btn_apply)
        root.addWidget(map_box)

        self.lbl_status = QLabel("")
        root.addWidget(self.lbl_status)

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
        self.btn_rescan.clicked.connect(self._on_rescan_devices)
        self.cmb_device.currentTextChanged.connect(self._on_device_changed_pending)
        self.chk_rot_en.toggled.connect(lambda checked: self.spn_rot_deg.setEnabled(bool(checked)))
        self.btn_apply.clicked.connect(self._on_apply_mapping)

    def _hbox(self, *widgets: QWidget) -> QWidget:
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        for it in widgets:
            l.addWidget(it)
        return w

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
        self._update_status()

    def _update_status(self) -> None:
        if self._demo_reason:
            self.lbl_status.setText(f"Demo mode: {self._demo_reason}")
            return
        en = "ON" if self._enabled else "OFF"
        readback_state = "cached/uncertain" if self._readback_uncertain else "measured"
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

    def closeEvent(self, e: Any) -> None:  # type: ignore[override]
        try:
            self._ramp_timer.stop()
            LOGGER.info("Closing scanner UI without altering current AO outputs.")
            self._daq.close()
        finally:
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
    app = QApplication.instance() or QApplication(sys.argv)
    _ensure_qt_in_notebook()
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    win.resize(980, 520)
    win.show()
    return app, win


def run(
    dev_name: str = "Dev1",
    ao_x: str = "ao0",
    ao_y: str = "ao1",
    *,
    return_app_and_window: bool = False,
    block: bool = True,
) -> tuple[QApplication, DaqXYWindow] | None:
    app = QApplication.instance() or QApplication(sys.argv)
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    win.resize(980, 520)
    win.show()
    if return_app_and_window:
        return app, win
    if not block:
        _ensure_qt_in_notebook()
        return app, win
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
