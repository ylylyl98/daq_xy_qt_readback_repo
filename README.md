# DAQ XY Qt Readback

PyQt6 desktop UI for controlling NI-DAQ analog outputs (`AO0`/`AO1`) from an XY pad, sliders, and nudge buttons.

- Output transitions are ramped (no instant jumps).
- Includes `Home` and `Ground (ramp to 0 V)` actions.
- Uses `iv_automation.DaqControl` with a real NI-DAQ device (no simulator fallback).
- Uses a persistent local controller process that owns the AO task, so closing/reopening the UI does not move outputs.

## Run the app

### Windows one-click launcher

Double-click `Start_DAQ_XY_UI.bat`.

It will:
1. Create `.venv/` if missing.
2. Install dependencies from `requirements.txt`.
3. Launch the app.

Optional arguments:

```bat
Start_DAQ_XY_UI.bat Dev1 ao0 ao1
Start_DAQ_XY_UI.bat Dev1 ao0 ao1 coord_config.json
```

### Manual run (from repo root)

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
$env:PYTHONPATH="src"
python -m daq_xy_qt_readback --dev Dev1 --ao-x ao0 --ao-y ao1
python -m daq_xy_qt_readback --dev Dev1 --ao-x ao0 --ao-y ao1 --coord-config coord_config.json
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
  src/daq_xy_qt_readback/
    __main__.py
    daq_xy_qt_readback.py
  scripts/
    smoke_check.py
  Start_DAQ_XY_UI.bat
  requirements.txt
  pyproject.toml
```

## Hardware wrapper (`iv_automation`)

For real hardware I/O, `iv_automation.py` must be importable in the same environment.
If unavailable, app startup fails with a clear error.

## Coordinate transform config (mapping + polarity + rotation)

Default behavior is backward compatible:
- rotation = `0`
- `axis_map.u=ao0`, `axis_map.v=ao1`
- `axis_polarity` normal for both axes
- range `0..10`
- out-of-range mode `clamp`

Optional JSON file:

```json
{
  "axis_map": { "u": "ao1", "v": "ao0" },
  "axis_polarity": { "u": "inverted", "v": "normal" },
  "voltage_range": { "min": 0.0, "max": 10.0 },
  "transform": {
    "rotation_deg": 90.0,
    "rotate_about": "origin",
    "offset_uv": [0.0, 0.0]
  },
  "on_out_of_range": "error"
}
```

Notes:
- Pipeline is centralized and applied to all manual scanner moves:
  logical `(x,y)` -> physical `(u,v)` -> AO channel voltages.
- `rotate_about` supports `origin` or `center` (center = midpoint of configured voltage range).

## Safety / Restart behavior

- UI startup does **not** write AO outputs.
- UI reads controller state/config and initializes widgets with signal blocking.
- Controller process keeps NI-DAQ AO task ownership while UI closes/reopens.
- AO changes only when output is enabled and a move/jog command is issued.
- Close the UI window does not reset AO.

Persisted files (Windows default `%APPDATA%\daq_xy_qt_readback`):
- `coord_config.json` (mapping, inversion, rotation, range, behavior)
- `scanner_state.json` (last logical and AO outputs)

## Troubleshooting

- `No module named daq_xy_qt_readback`: set `PYTHONPATH=src` (manual mode) or use `pip install -e .`.
- `No module named PyQt6`: run `python -m pip install -r requirements.txt`.
- `Real DAQ control is unavailable`: install/import `iv_automation.py` dependencies (`nidaqmx`, `pyvisa`, `numpy`) in the same environment.
- Window fails to open with DAQ errors: verify device/channel names (`--dev`, `--ao-x`, `--ao-y`) and NI-DAQ availability.
- If GUI launch works but hardware writes do not: confirm `iv_automation.DaqControl` methods are available in your environment.
