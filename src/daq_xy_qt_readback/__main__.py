"""CLI entrypoint for the DAQ XY Qt UI."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from .daq_xy_qt_readback import _DAQ_IMPORT_ERROR, _RealDaqControl, run
from ._version import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and launch the UI."""
    p = argparse.ArgumentParser(description="DAQ XY PyQt6 UI (ramped output).")
    p.add_argument("--version", action="version", version=f"DAQ XY Control {__version__}")
    p.add_argument("--packaged-smoke-test", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--packaged-backend-check", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--dev", default="Dev1", help="NI device name (default: Dev1)")
    p.add_argument("--ao-x", default="ao0", help="AO channel for X (default: ao0)")
    p.add_argument("--ao-y", default="ao1", help="AO channel for Y (default: ao1)")
    args = p.parse_args(argv)
    if args.packaged_backend_check:
        if _RealDaqControl is None:
            print(f"DAQ backend import failed: {_DAQ_IMPORT_ERROR!r}", file=sys.stderr)
            return 2
        print("DAQ backend import: OK")
        return 0
    if args.packaged_smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DAQ_XY_DISABLE_UPDATE_CHECK", "1")
        from PyQt6.QtWidgets import QApplication

        from .coordinate_transform import MappingSettings
        from .daq_xy_qt_readback import DaqXYWindow

        app = QApplication.instance() or QApplication([])
        win = DaqXYWindow(
            dev_name="Dev1",
            ao_x="ao0",
            ao_y="ao1",
            mapping=MappingSettings(),
            devices=[],
            channels_by_device={},
            demo_reason="Packaged application smoke test",
        )
        win.show()
        app.processEvents()
        win.close()
        app.processEvents()
        return 0
    try:
        run(args.dev, args.ao_x, args.ao_y)
    except Exception as exc:
        print(f"Failed to launch DAQ XY UI: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
