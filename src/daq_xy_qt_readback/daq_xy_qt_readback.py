
"""
daq_xy_qt.py - PyQt6 UI to control NI-DAQ AO0/AO1 (0-10 V)
with an XY pad, sliders, and nudge buttons.

Safety constraints:
- **No Emergency Stop** (to avoid sudden jumps).
- **All moves always ramp** with fixed parameters:
    step = 0.1 V per update
    dwell = 0.05 s between steps  (50 ms timer)
- **Ground** button ramps both X and Y to 0 V using the same ramp (never instant).

Notebook usage (non-blocking):
    from daq_xy_qt import launch
    app, win = launch("Dev1", "ao0", "ao1")

Script usage (blocking):
    from daq_xy_qt import run
    run("Dev1", "ao0", "ao1")                 # blocks until window closes
    # or non-blocking:
    app, win = run("Dev1", "ao0", "ao1", block=False)

Requires:
- PyQt6 (pip install PyQt6)
- Your iv_automation.py providing DaqControl for a real NI-DAQ device.
"""

from __future__ import annotations

import sys
import logging
from dataclasses import dataclass
from typing import Any, Tuple

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSlider, QDoubleSpinBox, QGroupBox, QFormLayout, QCheckBox
)
from .coordinate_transform import (
    CoordinateSettings,
    ao_volts_to_physical_uv,
    load_coordinate_settings,
    logical_xy_to_physical_uv,
    physical_uv_to_ao_volts,
    physical_uv_to_logical_xy,
)

STEP_PER_MOVE = 0.05
LOGGER = logging.getLogger(__name__)

# ---------------- DAQ backend ----------------
def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, float(v)))

try:
    from iv_automation import DaqControl as _RealDaqControl  # real hardware wrapper
    _DAQ_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    _RealDaqControl = None
    _DAQ_IMPORT_ERROR = exc


def _next_ramp_point(
    x: float,
    y: float,
    target_x: float,
    target_y: float,
    step: float,
) -> Tuple[float, float]:
    """Compute one ramp step from current point toward target."""
    dx = target_x - x
    dy = target_y - y
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= step:
        return target_x, target_y
    return x + (dx / dist) * step, y + (dy / dist) * step


class DaqInterface:
    """Thin adapter around the DAQ backend with safe read fallbacks."""

    def __init__(
        self,
        dev_name: str,
        ao_x: str,
        ao_y: str,
        coord_settings: CoordinateSettings | None = None,
    ) -> None:
        if _RealDaqControl is None:
            raise RuntimeError(
                "Real DAQ control is unavailable. Ensure iv_automation.py and its "
                "dependencies (nidaqmx, pyvisa, numpy) are installed."
            ) from _DAQ_IMPORT_ERROR
        self.settings = coord_settings or CoordinateSettings.default_for_channels(ao_x, ao_y)
        self.settings.validate()
        self._daq = _RealDaqControl(dev_name)
        configured_channels = {ao_x, ao_y}
        mapped_channels = {self.settings.axis_map.u, self.settings.axis_map.v}
        if mapped_channels != configured_channels:
            raise ValueError(
                "axis_map must use exactly the configured channels "
                f"{sorted(configured_channels)}, got {sorted(mapped_channels)}"
            )
        self._channel_to_var = {
            ao_x: "ch0_v",
            ao_y: "ch1_v",
        }
        self._x, self._y = self.settings.voltage_range.min_v, self.settings.voltage_range.min_v
        self._daq.add_ao_channel(ao_x, self._channel_to_var[ao_x])
        self._daq.add_ao_channel(ao_y, self._channel_to_var[ao_y])
        self._receive()

    def read_measured_xy(self) -> Tuple[float, float]:
        try:
            self._daq.read_y()
            measured_ao = {
                ch: float(self._daq.send_y("measured_" + var_name))
                for ch, var_name in self._channel_to_var.items()
            }
            u, v = ao_volts_to_physical_uv(measured_ao, self.settings)
            x, y = physical_uv_to_logical_xy(u, v, self.settings)
            return _clamp(x), _clamp(y)
        except Exception:
            LOGGER.debug("Falling back to last set XY when measured readback fails.", exc_info=True)
            return self._x, self._y

    def _receive(self) -> None:
        u, v = logical_xy_to_physical_uv(self._x, self._y, self.settings)
        ao_volts, clamped = physical_uv_to_ao_volts(u, v, self.settings)
        if clamped:
            LOGGER.warning(
                "Coordinate command was clamped to voltage range [%s, %s].",
                self.settings.voltage_range.min_v,
                self.settings.voltage_range.max_v,
            )
        for channel, var_name in self._channel_to_var.items():
            self._daq.receive_x(var_name, ao_volts[channel])

    def write_xy(self, x_v: float, y_v: float) -> Tuple[float, float]:
        self._x = _clamp(x_v, self.settings.voltage_range.min_v, self.settings.voltage_range.max_v)
        self._y = _clamp(y_v, self.settings.voltage_range.min_v, self.settings.voltage_range.max_v)
        self._receive()
        self._daq.write_x()
        try:
            self._daq.read_y()
        except Exception:
            LOGGER.debug("DAQ readback failed after write; continuing with setpoints.", exc_info=True)
        return self._x, self._y

    def close(self) -> None:
        try:
            self._daq.ao_task.close()
        except Exception:
            LOGGER.debug("Failed closing AO task.", exc_info=True)
        try:
            self._daq.ai_task.close()
        except Exception:
            LOGGER.debug("Failed closing AI task.", exc_info=True)


# ---------------- XY Pad ----------------
class XyPad(QWidget):
    clicked = pyqtSignal(float, float)  # volts (x, y)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self._vmin = 0.0
        self._vmax = 10.0
        self._x, self._y = 0.0, 0.0

    def set_voltage_range(self, vmin: float, vmax: float) -> None:
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._x = _clamp(self._x, self._vmin, self._vmax)
        self._y = _clamp(self._y, self._vmin, self._vmax)
        self.update()

    def set_xy(self, x_v: float, y_v: float) -> None:
        self._x, self._y = _clamp(x_v, self._vmin, self._vmax), _clamp(y_v, self._vmin, self._vmax)
        self.update()

    def mousePressEvent(self, e: Any) -> None:  # type: ignore[override]
        self._handle_mouse(e)

    def mouseMoveEvent(self, e: Any) -> None:  # type: ignore[override]
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._handle_mouse(e)

    def _handle_mouse(self, e: Any) -> None:
        w = max(1, self.width() - 1)
        h = max(1, self.height() - 1)
        span = max(1e-9, self._vmax - self._vmin)
        pos = e.position()  # QPointF in PyQt6
        x_v = _clamp(self._vmin + span * (pos.x() / w), self._vmin, self._vmax)
        y_v = _clamp(self._vmin + span * (1.0 - (pos.y() / h)), self._vmin, self._vmax)
        self.clicked.emit(float(x_v), float(y_v))

    def paintEvent(self, _: Any) -> None:  # type: ignore[override]
        p = QPainter(self)
        w, h = self.width(), self.height()
        # Border
        pen = QPen()
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(0, 0, w-1, h-1)
        # Grid 5x5
        for i in range(1, 5):
            xi = int(i * w / 5)
            yi = int(i * h / 5)
            p.drawLine(xi, 0, xi, h)
            p.drawLine(0, yi, w, yi)
        # Crosshair at current XY
        span = max(1e-9, self._vmax - self._vmin)
        cx = int(((self._x - self._vmin) / span) * (w - 1))
        cy = int((1.0 - ((self._y - self._vmin) / span)) * (h - 1))
        p.drawLine(cx-6, cy, cx+6, cy)
        p.drawLine(cx, cy-6, cx, cy+6)


# ---------------- Main Window ----------------
@dataclass
class RampConfig:
    step_v: float = STEP_PER_MOVE     # fixed step (V)
    dwell_ms: int = 100      # fixed dwell (ms)

class DaqXYWindow(QMainWindow):
    """Main UI window with ramped XY output controls."""

    def __init__(
        self,
        dev_name: str = "Dev1",
        ao_x: str = "ao0",
        ao_y: str = "ao1",
        coord_settings: CoordinateSettings | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"NI-DAQ XY Control (AO0/AO1) - Ramped {STEP_PER_MOVE}V @ 100ms")
        self._enabled = False
        self._daq = DaqInterface(dev_name, ao_x, ao_y, coord_settings=coord_settings)
        self._vmin = self._daq.settings.voltage_range.min_v
        self._vmax = self._daq.settings.voltage_range.max_v
        mx, my = self._daq.read_measured_xy()
        self._x = mx
        self._y = my
        self._target_x = mx
        self._target_y = my
        self.ramp = RampConfig()
        self._ramp_timer = QTimer(self)
        self._ramp_timer.timeout.connect(self._ramp_step)

        self._build_ui()
        self._sync_ui()

    # ---- UI construction ----
    def _build_ui(self) -> None:
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)

        # Top controls (no Emergency Stop)
        top = QHBoxLayout()
        self.chk_enable = QCheckBox("Enable Output")
        self.btn_home = QPushButton("Home (5V,5V)")
        self.btn_ground = QPushButton("Ground (ramp to 0V)")
        top.addWidget(self.chk_enable)
        top.addWidget(self.btn_home)
        top.addWidget(self.btn_ground)
        top.addStretch(1)
        root.addLayout(top)

        # Center: XY pad + sliders + nudge (fixed 0.1 V)
        center = QHBoxLayout()
        # XY pad
        self.pad = XyPad()
        self.pad.set_voltage_range(self._vmin, self._vmax)
        center.addWidget(self.pad, 2)

        # Sliders + spins
        sliders_box = QGroupBox("Voltages")
        fl = QFormLayout(sliders_box)

        self.sld_x = QSlider(Qt.Orientation.Horizontal); self.sld_x.setRange(0, 1000)
        self.sld_y = QSlider(Qt.Orientation.Horizontal); self.sld_y.setRange(0, 1000)
        self.spn_x = QDoubleSpinBox(); self.spn_x.setRange(self._vmin, self._vmax); self.spn_x.setDecimals(3); self.spn_x.setSingleStep(STEP_PER_MOVE)
        self.spn_y = QDoubleSpinBox(); self.spn_y.setRange(self._vmin, self._vmax); self.spn_y.setDecimals(3); self.spn_y.setSingleStep(STEP_PER_MOVE)

        fl.addRow(QLabel("X (V)"), self._hbox(self.sld_x, self.spn_x))
        fl.addRow(QLabel("Y (V)"), self._hbox(self.sld_y, self.spn_y))

        center.addWidget(sliders_box, 3)

        # Nudge (fixed 0.1 V)
        right_box = QGroupBox(f"Nudge (+/-{STEP_PER_MOVE} V each)")
        rgrid = QGridLayout(right_box)
        self.btn_left  = QPushButton("Left")
        self.btn_right = QPushButton("Right")
        self.btn_up    = QPushButton("Up")
        self.btn_down  = QPushButton("Down")

        rgrid.addWidget(self.btn_up,          0, 1)
        rgrid.addWidget(self.btn_left,        1, 0)
        rgrid.addWidget(self.btn_right,       1, 2)
        rgrid.addWidget(self.btn_down,        2, 1)

        center.addWidget(right_box, 2)
        root.addLayout(center)

        # Status
        self.lbl_status = QLabel("")
        root.addWidget(self.lbl_status)

        # Connections
        self.chk_enable.toggled.connect(self._on_enable_toggled)
        self.btn_home.clicked.connect(lambda: self.set_target_volts(5.0, 5.0))
        self.btn_ground.clicked.connect(self.ground)
        self.pad.clicked.connect(lambda xv, yv: self.set_target_volts(xv, yv))

        self.sld_x.valueChanged.connect(lambda v: self._on_slider_spin_changed('x', self._slider_to_volts(v)))
        self.sld_y.valueChanged.connect(lambda v: self._on_slider_spin_changed('y', self._slider_to_volts(v)))
        self.spn_x.valueChanged.connect(lambda v: self._on_slider_spin_changed('x', v))
        self.spn_y.valueChanged.connect(lambda v: self._on_slider_spin_changed('y', v))

        self.btn_left.clicked.connect(lambda: self._nudge(-STEP_PER_MOVE,  0.0))
        self.btn_right.clicked.connect(lambda: self._nudge(+STEP_PER_MOVE,  0.0))
        self.btn_up.clicked.connect(lambda: self._nudge( 0.0,  +STEP_PER_MOVE))
        self.btn_down.clicked.connect(lambda: self._nudge( 0.0,  -STEP_PER_MOVE))

    def _hbox(self, *widgets: QWidget) -> QWidget:
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0,0,0,0)
        for x in widgets: l.addWidget(x)
        return w

    def _slider_to_volts(self, slider_value: int) -> float:
        return self._vmin + (float(slider_value) / 1000.0) * (self._vmax - self._vmin)

    def _volts_to_slider(self, volts: float) -> int:
        if self._vmax <= self._vmin:
            return 0
        ratio = (volts - self._vmin) / (self._vmax - self._vmin)
        return int(round(max(0.0, min(1.0, ratio)) * 1000.0))

    def _sync_ui(self) -> None:
        # sync drivers & widgets
        self.pad.set_xy(self._x, self._y)
        self.sld_x.blockSignals(True); self.sld_y.blockSignals(True)
        self.spn_x.blockSignals(True); self.spn_y.blockSignals(True)
        self.sld_x.setValue(self._volts_to_slider(self._x))
        self.sld_y.setValue(self._volts_to_slider(self._y))
        self.spn_x.setValue(self._x)
        self.spn_y.setValue(self._y)
        self.sld_x.blockSignals(False); self.sld_y.blockSignals(False)
        self.spn_x.blockSignals(False); self.spn_y.blockSignals(False)
        self._update_status()

    def _update_status(self) -> None:
        en = "ON" if self._enabled else "OFF"
        self.lbl_status.setText(f"Output {en} | X={self._x:.3f} V | Y={self._y:.3f} V (ramp {STEP_PER_MOVE}V/100ms)")

    # ---- Actions ----
    def _on_enable_toggled(self, checked: bool) -> None:
        self._enabled = bool(checked)
        # No forced zeroing on disable (to avoid sudden jumps)
        if not self._enabled:
            self._ramp_timer.stop()
        self._update_status()

    def _on_slider_spin_changed(self, axis: str, value: float) -> None:
        if axis == 'x':
            self.set_target_volts(value, self._target_y)
        else:
            self.set_target_volts(self._target_x, value)

    def _nudge(self, dx: float, dy: float) -> None:
        self.set_target_volts(self._target_x + dx, self._target_y + dy)

    def set_target_volts(self, x_v: float, y_v: float) -> None:
        x_v = _clamp(x_v, self._vmin, self._vmax); y_v = _clamp(y_v, self._vmin, self._vmax)
        self._target_x, self._target_y = x_v, y_v
        # reflect targets in UI immediately
        self.pad.set_xy(x_v, y_v)
        self.sld_x.blockSignals(True); self.sld_y.blockSignals(True)
        self.spn_x.blockSignals(True); self.spn_y.blockSignals(True)
        self.sld_x.setValue(self._volts_to_slider(x_v))
        self.sld_y.setValue(self._volts_to_slider(y_v))
        self.spn_x.setValue(x_v)
        self.spn_y.setValue(y_v)
        self.sld_x.blockSignals(False); self.sld_y.blockSignals(False)
        self.spn_x.blockSignals(False); self.spn_y.blockSignals(False)
        # start/continue ramp
        if self._enabled:
            self._start_ramp()

    def ground(self) -> None:
        """Ramp both channels to 0 V using fixed ramp parameters."""
        if not self._enabled:
            # Turn on output so the ramp can proceed safely.
            self.chk_enable.setChecked(True)
        self.set_target_volts(0.0, 0.0)

    # ---- ramping (fixed 0.1 V / 50 ms) ----
    def _start_ramp(self) -> None:
        if self.ramp.dwell_ms <= 0:
            self.ramp.dwell_ms = 50
        if not self._ramp_timer.isActive():
            self._ramp_timer.start(self.ramp.dwell_ms)

    def _ramp_step(self) -> None:
        if not self._enabled:
            self._ramp_timer.stop()
            return

        dx = self._target_x - self._x
        dy = self._target_y - self._y
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            self._ramp_timer.stop()
            return

        step = self.ramp.step_v  # 0.1 V fixed
        nx, ny = _next_ramp_point(self._x, self._y, self._target_x, self._target_y, step)

        self._x, self._y = self._daq.write_xy(_clamp(nx), _clamp(ny))
        self._sync_ui()

    # Clean close
    def closeEvent(self, e: Any) -> None:  # type: ignore[override]
        try:
            self._ramp_timer.stop()
            self._daq.close()
        finally:
            super().closeEvent(e)


# ---------------- Launchers ----------------
def _ensure_qt_in_notebook() -> None:
    """Enable IPython Qt integration if running in a notebook/console."""
    try:
        from IPython import get_ipython
        ip = get_ipython()
        if ip is not None:
            ip.run_line_magic("gui", "qt")
    except Exception:
        pass


def _auto_window(
    dev_name: str = "Dev1",
    ao_x: str = "ao0",
    ao_y: str = "ao1",
    coord_settings: CoordinateSettings | None = None,
) -> DaqXYWindow:
    # Try Dev1 then Dev2 if first attempt fails
    try:
        return DaqXYWindow(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y, coord_settings=coord_settings)
    except Exception as e1:
        if dev_name.lower() == "dev1":
            try:
                return DaqXYWindow(dev_name="Dev2", ao_x=ao_x, ao_y=ao_y, coord_settings=coord_settings)
            except Exception:
                raise e1
        raise


def launch(
    dev_name: str = "Dev1",
    ao_x: str = "ao0",
    ao_y: str = "ao1",
    coord_config_path: str | None = None,
) -> Tuple[QApplication, DaqXYWindow]:
    """Non-blocking launcher ideal for notebooks. Returns (app, window)."""
    app = QApplication.instance() or QApplication(sys.argv)
    _ensure_qt_in_notebook()
    settings = load_coordinate_settings(
        coord_config_path,
        CoordinateSettings.default_for_channels(ao_x, ao_y),
    )
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y, coord_settings=settings)
    win.resize(900, 420)
    win.show()
    return app, win

def run(
    dev_name: str = "Dev1",
    ao_x: str = "ao0",
    ao_y: str = "ao1",
    coord_config_path: str | None = None,
    *,
    return_app_and_window: bool = False,
    block: bool = True,
) -> Tuple[QApplication, DaqXYWindow] | None:
    """
    General launcher.
      - return_app_and_window=True returns (app, win) and does not call exec.
      - block=False is non-blocking (good for notebooks).
      - block=True (default) enters the Qt event loop (script-style).
    """
    app = QApplication.instance() or QApplication(sys.argv)
    settings = load_coordinate_settings(
        coord_config_path,
        CoordinateSettings.default_for_channels(ao_x, ao_y),
    )
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y, coord_settings=settings)
    win.resize(900, 420)
    win.show()

    if return_app_and_window:
        return app, win

    if not block:
        _ensure_qt_in_notebook()
        return app, win

    sys.exit(app.exec())

if __name__ == "__main__":
    run()


