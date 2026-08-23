# PyInstaller one-folder build for the Windows desktop application.

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


repo_root = Path(SPECPATH).parents[1]
src_root = repo_root / "src"
package_root = src_root / "daq_xy_qt_readback"
generated_version = repo_root / "build" / "generated" / "file_version_info.txt"

hidden_imports = ["iv_automation"]
for package in ("nidaqmx", "pyvisa", "serial"):
    hidden_imports.extend(collect_submodules(package))

datas = [
    (str(package_root / "assets"), "daq_xy_qt_readback/assets"),
]
datas.extend(collect_data_files("nidaqmx"))
# NI's Python packages read their installed distribution versions during import.
# PyInstaller collects the modules but not these metadata records automatically.
for distribution in ("nidaqmx", "nitypes", "pyvisa"):
    datas.extend(copy_metadata(distribution))

a = Analysis(
    [str(repo_root / "scripts" / "daq_xy_control_entry.py")],
    pathex=[str(repo_root), str(src_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "jupyter", "matplotlib", "notebook"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DAQ XY Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=os.environ.get("DAQ_XY_BUILD_CONSOLE") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(package_root / "assets" / "daq_xy_control_unique.ico"),
    version=str(generated_version),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DAQ XY Control",
)
