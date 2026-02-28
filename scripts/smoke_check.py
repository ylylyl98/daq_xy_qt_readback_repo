"""Minimal smoke check for local development."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    sys.path.insert(0, str(src_dir))

    import daq_xy_qt_readback
    from daq_xy_qt_readback import __main__ as cli
    from daq_xy_qt_readback.daq_xy_qt_readback import DaqInterface

    assert callable(daq_xy_qt_readback.run)
    assert callable(daq_xy_qt_readback.launch)
    assert DaqInterface is not None

    try:
        cli.main(["--help"])
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise

    print("smoke-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
