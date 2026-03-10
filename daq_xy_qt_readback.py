
"""
daq_xy_qt.py — PyQt6 UI to control NI-DAQ AO0/AO1 (0–10 V)
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
- Your iv_automation.py providing DaqControl (or a simulator is used automatically).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Tuple

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSlider, QDoubleSpinBox, QGroupBox, QFormLayout, QCheckBox
)

# ---------------- DAQ backend ----------------
def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, float(v)))

class _SimDaq:
    """Simulator (no hardware I/O) if iv_automation is unavailable."""
    def __init__(self, dev_name: str) -> None:
        self.last = {"x_v": 0.0, "y_v": 0.0}
        print("[SimDaq] Using simulator (no hardware).")

    def add_ao_channel(self, ch: str, name: str) -> None:
        print(f"[SimDaq] add_ao_channel({ch!r}, {name!r})")

    def receive_x(self, name: str, value: float) -> None:
        self.last[name] = float(value)

    def write_x(self) -> None:
        print(f"[SimDaq] write AO -> x={self.last['x_v']:.3f} V, y={self.last['y_v']:.3f} V")

    def read_y(self):
        return self.last

    @property
    def ao_task(self):
        class _T:
            def close(self_inner): print("[SimDaq] ao_task.close()")
        return _T()

    @property
    def ai_task(self):
        class _T:
            def close(self_inner): print("[SimDaq] ai_task.close()")
        return _T()

try:
    from iv_automation import DaqControl as _RealDaqControl  # your hardware wrapper
    _DAQ_AVAILABLE = True
except Exception:
    _RealDaqControl = _SimDaq  # fallback
    _DAQ_AVAILABLE = False


class DaqInterface:
    def __init__(self, dev_name: str, ao_x: str, ao_y: str) -> None:
        self._daq = _RealDaqControl(dev_name)
        self._name_x, self._name_y = "x_v", "y_v"
        self._x, self._y = 0.0, 0.0
        self._daq.add_ao_channel(ao_x, self._name_x)
        self._daq.add_ao_channel(ao_y, self._name_y)
        self._receive()

    def read_measured_xy(self):
        try:
            self._daq.read_y()
            mx = float(self._daq.send_y('measured_' + self._name_x))
            my = float(self._daq.send_y('measured_' + self._name_y))
            return _clamp(mx), _clamp(my)
        except Exception:
            return self._x, self._y

    def _receive(self):
        self._daq.receive_x(self._name_x, self._x)
        self._daq.receive_x(self._name_y, self._y)

    def write_xy(self, x_v: float, y_v: float) -> Tuple[float, float]:
        self._x, self._y = _clamp(x_v), _clamp(y_v)
        self._receive()
        self._daq.write_x()
        try: self._daq.read_y()
        except Exception: pass
        return self._x, self._y

    def close(self):
        try: self._daq.ao_task.close()
        except Exception: pass
        try: self._daq.ai_task.close()
        except Exception: pass


# ---------------- XY Pad ----------------
class XyPad(QWidget):
    clicked = pyqtSignal(float, float)  # volts (x, y)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self._x, self._y = 0.0, 0.0

    def set_xy(self, x_v: float, y_v: float):
        self._x, self._y = _clamp(x_v), _clamp(y_v)
        self.update()

    def mousePressEvent(self, e):  # type: ignore[override]
        self._handle_mouse(e)

    def mouseMoveEvent(self, e):  # type: ignore[override]
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._handle_mouse(e)

    def _handle_mouse(self, e):
        w = max(1, self.width() - 1)
        h = max(1, self.height() - 1)
        pos = e.position()  # QPointF in PyQt6
        x_v = _clamp(10.0 * (pos.x() / w))
        y_v = _clamp(10.0 * (1.0 - (pos.y() / h)))
        self.clicked.emit(float(x_v), float(y_v))

    def paintEvent(self, _):  # type: ignore[override]
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
        cx = int((self._x / 10.0) * (w-1))
        cy = int((1.0 - self._y / 10.0) * (h-1))
        p.drawLine(cx-6, cy, cx+6, cy)
        p.drawLine(cx, cy-6, cx, cy+6)


# ---------------- Main Window ----------------
@dataclass
class RampConfig:
    step_v: float = 0.1     # fixed step (V)
    dwell_ms: int = 100      # fixed dwell (ms)

class DaqXYWindow(QMainWindow):
    def __init__(self, dev_name="Dev3", ao_x="ao0", ao_y="ao1"):
        super().__init__()
        self.setWindowTitle("NI-DAQ XY Control (AO0/AO1) — Ramped 0.1V @ 100ms")
        self._enabled = False
        self._daq = DaqInterface(dev_name, ao_x, ao_y)
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
    def _build_ui(self):
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
        center.addWidget(self.pad, 2)

        # Sliders + spins
        sliders_box = QGroupBox("Voltages")
        fl = QFormLayout(sliders_box)

        self.sld_x = QSlider(Qt.Orientation.Horizontal); self.sld_x.setRange(0, 1000)
        self.sld_y = QSlider(Qt.Orientation.Horizontal); self.sld_y.setRange(0, 1000)
        self.spn_x = QDoubleSpinBox(); self.spn_x.setRange(0.0, 10.0); self.spn_x.setDecimals(3); self.spn_x.setSingleStep(0.1)
        self.spn_y = QDoubleSpinBox(); self.spn_y.setRange(0.0, 10.0); self.spn_y.setDecimals(3); self.spn_y.setSingleStep(0.1)

        fl.addRow(QLabel("X (V)"), self._hbox(self.sld_x, self.spn_x))
        fl.addRow(QLabel("Y (V)"), self._hbox(self.sld_y, self.spn_y))

        center.addWidget(sliders_box, 3)

        # Nudge (fixed 0.1 V)
        right_box = QGroupBox("Nudge (±0.1 V each)")
        rgrid = QGridLayout(right_box)
        self.btn_left  = QPushButton("◀")
        self.btn_right = QPushButton("▶")
        self.btn_up    = QPushButton("▲")
        self.btn_down  = QPushButton("▼")

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

        self.sld_x.valueChanged.connect(lambda v: self._on_slider_spin_changed('x', v/100.0))
        self.sld_y.valueChanged.connect(lambda v: self._on_slider_spin_changed('y', v/100.0))
        self.spn_x.valueChanged.connect(lambda v: self._on_slider_spin_changed('x', v))
        self.spn_y.valueChanged.connect(lambda v: self._on_slider_spin_changed('y', v))

        self.btn_left.clicked.connect(lambda: self._nudge(-0.1,  0.0))
        self.btn_right.clicked.connect(lambda: self._nudge(+0.1,  0.0))
        self.btn_up.clicked.connect(lambda: self._nudge( 0.0,  +0.1))
        self.btn_down.clicked.connect(lambda: self._nudge( 0.0,  -0.1))

    def _hbox(self, *widgets):
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0,0,0,0)
        for x in widgets: l.addWidget(x)
        return w

    def _sync_ui(self):
        # sync drivers & widgets
        self.pad.set_xy(self._x, self._y)
        self.sld_x.blockSignals(True); self.sld_y.blockSignals(True)
        self.spn_x.blockSignals(True); self.spn_y.blockSignals(True)
        self.sld_x.setValue(int(round(self._x * 100)))
        self.sld_y.setValue(int(round(self._y * 100)))
        self.spn_x.setValue(self._x)
        self.spn_y.setValue(self._y)
        self.sld_x.blockSignals(False); self.sld_y.blockSignals(False)
        self.spn_x.blockSignals(False); self.spn_y.blockSignals(False)
        self._update_status()

    def _update_status(self):
        en = "ON" if self._enabled else "OFF"
        self.lbl_status.setText(f"Output {en} | X={self._x:.3f} V | Y={self._y:.3f} V (ramp 0.1V/100ms)")

    # ---- Actions ----
    def _on_enable_toggled(self, checked: bool):
        self._enabled = bool(checked)
        # No forced zeroing on disable (to avoid sudden jumps)
        if not self._enabled:
            self._ramp_timer.stop()
        self._update_status()

    def _on_slider_spin_changed(self, axis: str, value: float):
        if axis == 'x':
            self.set_target_volts(value, self._target_y)
        else:
            self.set_target_volts(self._target_x, value)

    def _nudge(self, dx: float, dy: float):
        self.set_target_volts(self._target_x + dx, self._target_y + dy)

    def set_target_volts(self, x_v: float, y_v: float):
        x_v = _clamp(x_v); y_v = _clamp(y_v)
        self._target_x, self._target_y = x_v, y_v
        # reflect targets in UI immediately
        self.pad.set_xy(x_v, y_v)
        self.sld_x.blockSignals(True); self.sld_y.blockSignals(True)
        self.spn_x.blockSignals(True); self.spn_y.blockSignals(True)
        self.sld_x.setValue(int(round(x_v * 100)))
        self.sld_y.setValue(int(round(y_v * 100)))
        self.spn_x.setValue(x_v)
        self.spn_y.setValue(y_v)
        self.sld_x.blockSignals(False); self.sld_y.blockSignals(False)
        self.spn_x.blockSignals(False); self.spn_y.blockSignals(False)
        # start/continue ramp
        if self._enabled:
            self._start_ramp()

    def ground(self):
        """Ramp both channels to 0 V using fixed ramp parameters."""
        if not self._enabled:
            # Turn on output so the ramp can proceed safely.
            self.chk_enable.setChecked(True)
        self.set_target_volts(0.0, 0.0)

    # ---- ramping (fixed 0.1 V / 50 ms) ----
    def _start_ramp(self):
        if self.ramp.dwell_ms <= 0:
            self.ramp.dwell_ms = 50
        if not self._ramp_timer.isActive():
            self._ramp_timer.start(self.ramp.dwell_ms)

    def _ramp_step(self):
        if not self._enabled:
            self._ramp_timer.stop()
            return

        dx = self._target_x - self._x
        dy = self._target_y - self._y
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            self._ramp_timer.stop()
            return

        step = self.ramp.step_v  # 0.1 V fixed
        # Move along the vector toward target by at most step
        dist = (dx*dx + dy*dy) ** 0.5
        if dist <= step:
            nx, ny = self._target_x, self._target_y
        else:
            nx = self._x + (dx / dist) * step
            ny = self._y + (dy / dist) * step

        self._x, self._y = self._daq.write_xy(_clamp(nx), _clamp(ny))
        self.pad.set_xy(self._x, self._y)
        self._sync_ui()

    # Clean close
    def closeEvent(self, e):  # type: ignore[override]
        try:
            self._ramp_timer.stop()
            self._daq.close()
        finally:
            super().closeEvent(e)


# ---------------- Launchers ----------------
def _ensure_qt_in_notebook():
    """Enable IPython Qt integration if running in a notebook/console."""
    try:
        from IPython import get_ipython
        ip = get_ipython()
        if ip is not None:
            ip.run_line_magic("gui", "qt")
    except Exception:
        pass


def _auto_window(dev_name="Dev1", ao_x="ao0", ao_y="ao1"):
    # Try Dev1 then Dev2 if first attempt fails
    try:
        return DaqXYWindow(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    except Exception as e1:
        if dev_name.lower() == "dev3":
            try:
                return DaqXYWindow(dev_name="Dev2", ao_x=ao_x, ao_y=ao_y)
            except Exception:
                raise e1
        raise


def launch(dev_name="Dev3", ao_x="ao0", ao_y="ao1"):
    """Non-blocking launcher ideal for notebooks. Returns (app, window)."""
    app = QApplication.instance() or QApplication(sys.argv)
    _ensure_qt_in_notebook()
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
    win.resize(900, 420)
    win.show()
    return app, win

def run(dev_name="Dev3", ao_x="ao0", ao_y="ao1", *, return_app_and_window=False, block=True):
    """
    General launcher.
      - return_app_and_window=True returns (app, win) and does not call exec.
      - block=False is non-blocking (good for notebooks).
      - block=True (default) enters the Qt event loop (script-style).
    """
    app = QApplication.instance() or QApplication(sys.argv)
    win = _auto_window(dev_name=dev_name, ao_x=ao_x, ao_y=ao_y)
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
