# Releasing DAQ XY Control for Windows

## Distribution design

Each release contains:

- A per-user Windows installer built with Inno Setup.
- A portable ZIP of the PyInstaller one-folder bundle.
- SHA-256 checksums for both downloads.

Python and application libraries are bundled. NI-DAQmx and device-specific Windows
drivers are not bundled and must be installed separately on target laboratory PCs.

The installer uses `%LOCALAPPDATA%\Programs\DAQ XY Control`, so installing the
application itself does not require administrator access. Settings remain under
`%APPDATA%\daq_xy_qt_readback` and are not replaced during an upgrade.

## Versioning

`src/daq_xy_qt_readback/_version.py` is the only application-version source.
Update it before creating a release:

```python
__version__ = "1.2.0"
```

The Git tag must match exactly with a leading `v`, for example `v1.2.0`. The release
workflow rejects mismatched tags.

## Local validation

From a clean Windows virtual environment:

```powershell
python -m pip install . pyinstaller
python -m unittest discover -s tests
python scripts/smoke_check.py
python scripts/build_windows.py
& "dist\DAQ XY Control\DAQ XY Control.exe" --version
& "dist\DAQ XY Control\DAQ XY Control.exe" --packaged-smoke-test
```

If Inno Setup 6 is installed:

```powershell
python scripts/build_windows.py --installer
```

Test the resulting installer on both a DAQ-only computer and a DAQ-plus-ANC300
computer. Confirm that no port is opened and no hardware output changes on startup.

## Publishing

After merging the release changes to `main`:

```powershell
git tag v1.2.0
git push origin v1.2.0
```

The `Release Windows installer` workflow tests the source, builds the packaged app,
compiles the installer, creates a portable ZIP and checksums, and publishes a GitHub
Release. The application update checker reads only the latest non-draft, non-prerelease
release returned by GitHub.

## Update behavior

- Automatic checks start after the UI and run no more than once every 24 hours.
- Checks use a four-second timeout and never run in the hardware worker threads.
- Offline or failed checks do not affect scanner or positioner controls.
- The app never downloads or executes an installer automatically.
- `View update` opens only this repository's trusted HTTPS release page.
- The installer mutex prevents replacing files while the app is running.

Set `DAQ_XY_DISABLE_UPDATE_CHECK=1` to disable automatic checks for an offline or
isolated installation. Manual checks remain available from About.

## Signing before broad distribution

Before publishing outside the internal test group, sign both `DAQ XY Control.exe` and
the installer with the organization's Windows code-signing certificate. Add signing
between the build and release steps without placing certificate secrets in the
repository. Verify the signature and SHA-256 assets before publishing.

## Pilot checklist

1. Install on a clean Windows 10 or Windows 11 PC without Python.
2. Verify clear behavior when NI-DAQmx is absent.
3. Verify DAQ-only operation with the positioner disabled.
4. Verify optional ANC300 setup without automatic connection.
5. Upgrade over the preceding version and confirm all settings remain.
6. Attempt installation while the app is open and confirm the installer asks for it
   to be closed.
7. Verify startup and update checking never write DAQ outputs or send serial motion.
8. Verify the app starts normally without internet access.
