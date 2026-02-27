import argparse
from .daq_xy_qt_readback import run

def main():
    p = argparse.ArgumentParser(description="DAQ XY PyQt6 UI (ramped output).")
    p.add_argument("--dev", default="Dev1", help="NI device name (default: Dev1)")
    p.add_argument("--ao-x", default="ao0", help="AO channel for X (default: ao0)")
    p.add_argument("--ao-y", default="ao1", help="AO channel for Y (default: ao1)")
    args = p.parse_args()
    run(args.dev, args.ao_x, args.ao_y)

if __name__ == "__main__":
    main()
