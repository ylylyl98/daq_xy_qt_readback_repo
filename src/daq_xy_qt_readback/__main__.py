"""CLI entrypoint for the DAQ XY Qt UI."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .daq_xy_qt_readback import run


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and launch the UI."""
    p = argparse.ArgumentParser(description="DAQ XY PyQt6 UI (ramped output).")
    p.add_argument("--dev", default="Dev1", help="NI device name (default: Dev1)")
    p.add_argument("--ao-x", default="ao0", help="AO channel for X (default: ao0)")
    p.add_argument("--ao-y", default="ao1", help="AO channel for Y (default: ao1)")
    p.add_argument(
        "--coord-config",
        default=None,
        help="Optional JSON config path for axis map/polarity/rotation settings.",
    )
    args = p.parse_args(argv)
    try:
        run(args.dev, args.ao_x, args.ao_y, coord_config_path=args.coord_config)
    except Exception as exc:
        print(f"Failed to launch DAQ XY UI: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
