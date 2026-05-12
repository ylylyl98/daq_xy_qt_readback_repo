# DAQ XY Qt Readback

PyQt6 desktop UI for controlling NI-DAQ analog outputs (`AO0`/`AO1`) from an XY pad, sliders, and nudge buttons.

- Output transitions are ramped (no instant jumps).
- Includes `Home` and `Ground (ramp to 0 V)` actions.
- Uses `iv_automation.DaqControl` with a real NI-DAQ device (no simulator fallback).
- Uses one bundled app icon for the Qt window and Windows taskbar identity.
- Uses a modern console layout with status badges, a larger XY pad, and separated control/setup panels.

## Run the app
```bat
git clone https://github.com/ylylyl98/daq_xy_qt_readback_repo.git
```

### Windows one-click launcher

Double-click `Start_DAQ_XY_UI.bat`.

It will:
1. Create `.venv/` if missing.
2. Install dependencies from `requirements.txt`.
3. Launch the app.

To create a desktop shortcut that uses the same app icon, double-click
`Create_DAQ_XY_Desktop_Shortcut.bat`.
Run it again after icon updates to refresh the shortcut target and icon path.

Optional arguments:

```bat
Start_DAQ_XY_UI.bat Dev1 ao0 ao1
```

### Manual run (from repo root)

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
$env:PYTHONPATH="src"
python -m daq_xy_qt_readback --dev Dev1 --ao-x ao0 --ao-y ao1
```

### Editable install path

```powershell
python -m pip install -e .
daq-xy-ui --dev Dev1 --ao-x ao0 --ao-y ao1
```

## Minimal smoke check

From repo root:

```powershell
python scripts\smoke_check.py
python -m compileall .
```

## Expected folder structure

```text
daq_xy_qt_readback_repo/
  Create_DAQ_XY_Desktop_Shortcut.bat
  Start_DAQ_XY_UI.bat
  src/daq_xy_qt_readback/
    assets/
      daq_xy_control_unique.ico
      daq_xy_control_unique.svg
      daq_xy_icon.ico
      daq_xy_icon.svg
    __main__.py
    daq_xy_qt_readback.py
  scripts/
    smoke_check.py
  requirements.txt
  pyproject.toml
```

## Hardware wrapper (`iv_automation`)

For real hardware I/O, `iv_automation.py` must be importable in the same environment.
If unavailable, app startup fails with a clear error.

## Troubleshooting

- `No module named daq_xy_qt_readback`: set `PYTHONPATH=src` (manual mode) or use `pip install -e .`.
- `No module named PyQt6`: run `python -m pip install -r requirements.txt`.
- `Real DAQ control is unavailable`: install/import `iv_automation.py` dependencies (`nidaqmx`, `pyvisa`, `numpy`) in the same environment.
- Window fails to open with DAQ errors: verify device/channel names (`--dev`, `--ao-x`, `--ao-y`) and NI-DAQ availability.
- If GUI launch works but hardware writes do not: confirm `iv_automation.DaqControl` methods are available in your environment.
