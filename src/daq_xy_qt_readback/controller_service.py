"""Controller service process with simple local JSON-over-TCP IPC."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import socketserver
from typing import Any

from .scanner_controller import ScannerController

LOGGER = logging.getLogger(__name__)


def _default_data_dir() -> Path:
    base = os.environ.get("DAQ_XY_DATA_DIR")
    if base:
        return Path(base)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "daq_xy_qt_readback"
    return Path.home() / ".daq_xy_qt_readback"


class _ThreadedTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            method = str(payload.get("method", ""))
            params = payload.get("params", {})
            result = self.server.dispatch(method, params)  # type: ignore[attr-defined]
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class _ControllerServer(_ThreadedTcpServer):
    def __init__(self, host: str, port: int, controller: ScannerController):
        super().__init__((host, port), _RequestHandler)
        self.controller = controller

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"status": "ok"}
        if method == "get_state":
            return self.controller.get_state()
        if method == "get_config":
            return self.controller.get_config()
        if method == "set_config":
            return self.controller.set_config(dict(params.get("config", {})))
        if method == "set_output":
            return self.controller.set_output(float(params["ao0_v"]), float(params["ao1_v"]))
        if method == "move_logical":
            return self.controller.move_logical(float(params["x"]), float(params["y"]))
        if method == "jog_logical":
            return self.controller.jog_logical(float(params["dx"]), float(params["dy"]))
        raise ValueError(f"Unknown method: {method}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAQ XY scanner controller service")
    parser.add_argument("--dev", default="Dev1")
    parser.add_argument("--ao-x", default="ao0")
    parser.add_argument("--ao-y", default="ao1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--fake-backend", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    controller = ScannerController(
        dev_name=args.dev,
        ao_x=args.ao_x,
        ao_y=args.ao_y,
        data_dir=Path(args.data_dir) if args.data_dir else _default_data_dir(),
        fake_backend=bool(args.fake_backend),
    )
    server = _ControllerServer(args.host, args.port, controller)
    LOGGER.info("Controller service started on %s:%s", args.host, args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
