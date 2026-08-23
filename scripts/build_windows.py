"""Build the self-contained Windows bundle and optional installer."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "packaging" / "windows" / "daq_xy_control.spec"
ISS_PATH = REPO_ROOT / "packaging" / "windows" / "daq_xy_control.iss"
GENERATED_DIR = REPO_ROOT / "build" / "generated"
VERSION_FILE = GENERATED_DIR / "file_version_info.txt"
BUNDLE_EXE = REPO_ROOT / "dist" / "DAQ XY Control" / "DAQ XY Control.exe"


def application_version() -> str:
    values = runpy.run_path(str(REPO_ROOT / "src" / "daq_xy_qt_readback" / "_version.py"))
    return str(values["__version__"])


def numeric_version(version: str) -> tuple[int, int, int, int]:
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts = [int(part) for part in core.split(".")]
    if not 1 <= len(parts) <= 4:
        raise ValueError(f"Version must contain one to four numeric parts: {version}")
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def write_windows_version_file(version: str) -> None:
    numbers = numeric_version(version)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Instrument Control'),
        StringStruct('FileDescription', 'DAQ XY Control'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'DAQ XY Control'),
        StringStruct('OriginalFilename', 'DAQ XY Control.exe'),
        StringStruct('ProductName', 'DAQ XY Control'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def find_iscc(explicit: str | None) -> Path | None:
    candidates = [
        explicit,
        os.environ.get("ISCC_PATH"),
        shutil.which("ISCC.exe"),
        str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe")
        if os.environ.get("LOCALAPPDATA")
        else None,
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", action="store_true", help="Also build the Inno Setup installer.")
    parser.add_argument("--console", action="store_true", help="Build a diagnostic console executable.")
    parser.add_argument("--iscc", help="Path to ISCC.exe when it is not installed in a standard location.")
    parser.add_argument("--expected-version", help="Fail unless this matches the application version.")
    args = parser.parse_args()

    version = application_version()
    if args.expected_version and args.expected_version.lstrip("v") != version:
        raise SystemExit(
            f"Release tag/version mismatch: expected {args.expected_version}, application is {version}."
        )
    write_windows_version_file(version)
    build_env = os.environ.copy()
    if args.console:
        build_env["DAQ_XY_BUILD_CONSOLE"] = "1"
    run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_PATH)],
        env=build_env,
    )
    if not BUNDLE_EXE.is_file():
        raise SystemExit(f"PyInstaller did not produce the expected executable: {BUNDLE_EXE}")

    if args.installer:
        iscc = find_iscc(args.iscc)
        if iscc is None:
            raise SystemExit("Inno Setup 6 was not found. Pass --iscc or set ISCC_PATH.")
        run([str(iscc), f"/DMyAppVersion={version}", str(ISS_PATH)])

    print(f"Built DAQ XY Control {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
