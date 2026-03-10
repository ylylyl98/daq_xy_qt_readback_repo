"""Minimal smoke check for local development."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    sys.path.insert(0, str(src_dir))

    import daq_xy_qt_readback
    from daq_xy_qt_readback import __main__ as cli
    from PyQt6.QtWidgets import QApplication
    from daq_xy_qt_readback.coordinate_transform import MappingSettings
    from daq_xy_qt_readback.daq_xy_qt_readback import DaqXYWindow

    assert callable(daq_xy_qt_readback.run)
    assert callable(daq_xy_qt_readback.launch)
    assert DaqXYWindow is not None

    try:
        cli.main(["--help"])
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise

    app = QApplication.instance() or QApplication([])
    win = DaqXYWindow(
        dev_name="Dev1",
        ao_x="ao0",
        ao_y="ao1",
        mapping=MappingSettings(),
        devices=[],
        channels_by_device={},
        demo_reason="smoke check",
    )
    win.show()
    app.processEvents()
    win.close()

    print("smoke-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
