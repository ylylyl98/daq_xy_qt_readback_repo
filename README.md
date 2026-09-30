# DAQ XY Qt Readback

PyQt6 desktop UI for controlling NI-DAQ analog outputs (`AO0`/`AO1`) from an XY pad, sliders, and nudge buttons.

- Output transitions are ramped (no instant jumps).
- Includes `Home` and `Ground (ramp to 0 V)` actions.
- Uses `iv_automation.DaqControl` with a real NI-DAQ device (no simulator fallback).
- Opens with the NI-DAQ disconnected; the Scanner panel has an explicit DAQ Connect/Disconnect control.
- Uses one bundled app icon for the Qt window and Windows taskbar identity.
- Uses a modern console layout with status badges, a larger XY pad, and separated control/setup panels.
- Optionally controls an ANC300 coarse XYZ positioner without affecting DAQ-only systems.
- Includes a compact, always-on-top directional controller for working beside PowerPoint or other applications.

## Run the app

### Install a released version

[Download DAQ XY Control 1.2.0 for Windows](https://github.com/ylylyl98/daq_xy_qt_readback_repo/releases/latest/download/DAQ-XY-Control-Setup-1.2.0.exe)

Run the downloaded installer. It includes Python, PyQt, and the application packages;
users do not need to install Python. Vendor hardware drivers, including NI-DAQmx,
remain separate prerequisites. The [GitHub Releases page](https://github.com/ylylyl98/daq_xy_qt_readback_repo/releases/latest)
also provides a portable ZIP and SHA-256 checksums.

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

Click **Compact** in the full window to switch to a small, always-on-top controller.
DAQ directional controls work independently of the ANC300 connection. Use
**Enable Scanner** when you also need the verified DAQ-near-zero and ANC300
stepping-mode sequence. Each click keeps the existing ramped `0.05 V` real-space
nudge behavior.

- Click the center restore button or press `Esc` to return to the full interface.
- When the compact window has keyboard focus, the arrow keys nudge in the matching direction.
- Switching views does not reconnect the DAQ or change the current output or target.
- **Ground DAQ** ramps the scanner's hardware X/Y outputs to 0 V without changing
  ANC300 modes. It is available in both full and compact views.
- Closing with a connected DAQ asks **Ramp X/Y to 0 V and close?** Choose **Close**
  to ramp and verify zero before exiting, or **Cancel** (the default) to keep working.
  A failed or interrupted ramp keeps the app open. ANC300 modes are unchanged.
- Low-level connection cleanup does not command a voltage reset. A crash cannot
  execute the normal close sequence; retention of output voltage after a crash
  depends on the DAQ hardware and driver and must be verified on the instrument.

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

The DAQ and ANC300 connections are independent. Connecting or disconnecting either
device does not connect, disconnect, or change the outputs of the other device.
Combined **Enable Scanner** and **Ground Scanner + ANC300** operations require both
devices to be connected because those commands intentionally coordinate them.

Applying positioner settings sends no movement command. DAQ scanner operation remains
available when the positioner is disabled, absent, or disconnected.

After connecting, use **GROUND POSITIONER** in the **Positioner** tab to send the ANC300
`setm <axis> gnd` command to all configured axes. This disables their outputs and
connects them to chassis ground. Use **ENABLE POSITIONER** to explicitly return the
configured positioner axes to stepping mode before issuing movement commands.

The positioner controls are separate from the scanner controls. Configure the ANC300
scanner X/Y axes separately in Setup (defaults: axes 1/2). **Ground Scanner + ANC300** first
ramps DAQ AO0/AO1 to 0 V, then grounds only those mapped ANC300 scanner axes.
**Positioner → Ground Positioner** controls only the configured ANC300 positioner axes.

**Ground DAQ** needs only the DAQ connection and targets hardware X/Y 0 V regardless
of coordinate mapping. It replaces any active movement target, blocks competing
movement while ramping, and holds at zero after completion. **Stop Ramp** interrupts
the ramp at its current output.

In both layouts, **Center (X=5, Y=5)** sits between the DAQ direction buttons and
returns to the real-space center. **Stop Ramp** is directly below the direction pad.
The separate **Output control** group contains **Enable Scanner**, **DAQ → 0 V**,
and **DAQ → 0 V + ANC300 GND**; these retain the grounding behavior described above.

For combined DAQ + ANC300 operations, **Enable Scanner**
first verifies several consecutive DAQ readbacks inside the configured near-zero
tolerance, then enables ANC300 stepping. **Ground Scanner + ANC300** (shown as
**Ground DAQ + ANC300** in compact mode) locks movement,
ramps the DAQ command toward 0 V, waits for three stable near-zero readbacks, and only
then sends and verifies ANC300 GND mode. Readback uncertainty or any failed check
blocks the ANC300 mode change. The default tolerance is ±0.010 V and is configurable
in Setup.

Positioner motion requires an explicit **Enable Positioner** action. Grounding sends
STOP before switching each configured positioner axis to GND; movement never silently
re-enables a grounded positioner axis.

Both layouts offer **Step** and **Continuous** modes. Step sends the selected count
(X/Y: 1–1000; Z: 1–100). Continuous uses the ANC300 continuous stepping command:
hold a direction button to move and release it to stop. The step count is unused in
Continuous mode; speed follows the controller's existing frequency and amplitude.
Changing modes, hiding the held button, losing window focus, or closing the window
requests STOP. Other directions are locked while moving; STOP remains available.
Compact arrow-key shortcuts apply only in Step mode; Continuous uses the held buttons.

Finite moves stay busy for the interval estimated from step count / queried frequency
plus 250 ms, then send STOP and wait for acknowledgment before accepting another move.
This is a conservative timing guard, not position feedback or an arrival measurement.
Do not change the controller frequency externally during a move. Explicit busy/moving
rejections are stopped and shown in the status text without disconnecting after STOP
succeeds. Other command/communication failures remain errors. Stop requests are handled
after any serial exchange already in progress; a lost connection cannot guarantee a
hardware stop. These controls must be verified on the connected instrument before use.

## Troubleshooting

- `No module named daq_xy_qt_readback`: set `PYTHONPATH=src` (manual mode) or use `pip install -e .`.
- `No module named PyQt6`: run `python -m pip install -r requirements.txt`.
- `Real DAQ control is unavailable`: install/import `iv_automation.py` dependencies (`nidaqmx`, `pyvisa`, `numpy`) in the same environment.
- Window fails to open with DAQ errors: verify device/channel names (`--dev`, `--ao-x`, `--ao-y`) and NI-DAQ availability.
- If GUI launch works but hardware writes do not: confirm `iv_automation.DaqControl` methods are available in your environment.
