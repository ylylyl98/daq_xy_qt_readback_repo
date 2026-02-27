# DAQ XY Qt Readback

A **PyQt6** desktop UI for controlling NI-DAQ analog outputs (AO0/AO1) from an **XY pad**, sliders, and nudge buttons.

- Output is **ramped** (no instant jumps).
- Includes **Home** and **Ground (ramp to 0 V)** actions.
- Uses your `iv_automation.DaqControl` if available; otherwise falls back to a simulator.

## Quick start (Windows: one-click)

Double-click: **Start_DAQ_XY_UI.bat**

It will:
1) create a local virtualenv in `.venv/` (first run only)  
2) install dependencies from `requirements.txt`  
3) launch the UI

## Manual start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m daq_xy_qt_readback --dev Dev1 --ao-x ao0 --ao-y ao1
```

Or install as a package (editable):

```bash
pip install -e .
daq-xy-ui --dev Dev1 --ao-x ao0 --ao-y ao1
```

## Hardware wrapper (iv_automation)

The UI tries to import `iv_automation.DaqControl`. If you want real hardware I/O,
make sure `iv_automation.py` is importable (e.g., place it next to this repo,
or install it into the same Python environment).

## Notes

- This project contains your original script under `src/daq_xy_qt_readback/daq_xy_qt_readback.py`.
- A notebook is included in `notebooks/`.

