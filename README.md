# DAQ XY Qt Readback

PyQt6 desktop UI for controlling NI-DAQ analog outputs (`AO0`/`AO1`) from an XY pad, sliders, and nudge buttons.

- Output transitions are ramped (no instant jumps).
- Includes `Home` and `Ground (ramp to 0 V)` actions.
- Uses `iv_automation.DaqControl` with a real NI-DAQ device (no simulator fallback).
- Uses one bundled app icon for the Qt window and Windows taskbar identity.
- Uses a modern console layout with status badges, a larger XY pad, and separated control/setup panels.
- Optionally controls an ANC300 coarse XYZ positioner without affecting DAQ-only systems.
- Includes a compact, always-on-top directional controller for working beside PowerPoint or other applications.

## Run the app

### Install a released version

Download `DAQ-XY-Control-Setup-<version>.exe` from the repository's GitHub Releases
page and run it. The installer includes Python, PyQt, and the application packages;
users do not need to install Python. Vendor hardware drivers, including NI-DAQmx,
remain separate prerequisites.

The installed app checks for a newer stable GitHub Release in the background no more
than once per day. Update failures are silent and never affect hardware control. Use
**About → Check for updates** for a manual check. Updates are installed by closing the
app and running the newer installer; scanner and positioner settings are preserved.

### Run from source

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

## Build a Windows release

The reproducible packaging files are under `packaging/windows`. On Windows, install
the project and PyInstaller, then build the one-folder application:

```powershell
python -m pip install . pyinstaller
python scripts/build_windows.py
```

With Inno Setup 6 installed, build the installer too:

```powershell
python scripts/build_windows.py --installer
```

Detailed versioning, test, signing, and release instructions are in
[`docs/RELEASING.md`](docs/RELEASING.md). Pushing a matching version tag starts the
GitHub Actions Windows release workflow.

## Compact controller

Click **Compact** in the full window to switch to a small, always-on-top controller. Enable
**Output** before entering compact mode; the four directional buttons are intentionally
disabled while output is off. Each click keeps the existing ramped `0.05 V` real-space
nudge behavior.

- Click the center restore button or press `Esc` to return to the full interface.
- When the compact window has keyboard focus, the arrow keys nudge in the matching direction.
- Switching views does not reconnect the DAQ or change the current output or target.
- Closing the UI stops any unfinished ramp at its last actual position and does not ground,
  home, or otherwise write new AO values. Reopening reads those existing outputs back.

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
    anc300_positioner.py
    daq_xy_qt_readback.py
    update_checker.py
    _version.py
  packaging/windows/
    daq_xy_control.spec
    daq_xy_control.iss
  scripts/
    smoke_check.py
  requirements.txt
  pyproject.toml
```

## Hardware wrapper (`iv_automation`)

For real hardware I/O, `iv_automation.py` must be importable in the same environment.
If unavailable, app startup fails with a clear error.

## Optional ANC300 positioner

Positioner support is disabled by default and never opens a serial port automatically.
In the **Setup** tab, enable the positioner for the current PC, select its COM port,
assign unique ANC300 axes to X/Y/Z, and state which physical direction corresponds
to each axis's positive command. Apply saves these settings in the current Windows
user's application-data folder. Use the **Positioner** tab to connect explicitly and
move using physical direction labels.

Applying positioner settings sends no movement command. DAQ scanner operation remains
available when the positioner is disabled, absent, or disconnected.

## Troubleshooting

- `No module named daq_xy_qt_readback`: set `PYTHONPATH=src` (manual mode) or use `pip install -e .`.
- `No module named PyQt6`: run `python -m pip install -r requirements.txt`.
- `Real DAQ control is unavailable`: install/import `iv_automation.py` dependencies (`nidaqmx`, `pyvisa`, `numpy`) in the same environment.
- Window fails to open with DAQ errors: verify device/channel names (`--dev`, `--ao-x`, `--ao-y`) and NI-DAQ availability.
- If GUI launch works but hardware writes do not: confirm `iv_automation.DaqControl` methods are available in your environment.
