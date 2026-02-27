"""Client helpers for scanner controller IPC."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any


class ControllerClient:
    """Simple JSON-over-TCP client for controller service."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout_s: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {"method": method, "params": params or {}}
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            wire = json.dumps(payload).encode("utf-8") + b"\n"
            sock.sendall(wire)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        if not data:
            raise RuntimeError("No response from controller service")
        response = json.loads(data.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "Unknown controller error")))
        return response.get("result")

    def ping(self) -> bool:
        try:
            self.call("ping")
            return True
        except Exception:
            return False

    def get_state(self) -> dict[str, float]:
        return dict(self.call("get_state"))

    def get_config(self) -> dict[str, Any]:
        return dict(self.call("get_config"))

    def set_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return dict(self.call("set_config", {"config": config}))

    def set_output(self, ao0_v: float, ao1_v: float) -> dict[str, float]:
        return dict(self.call("set_output", {"ao0_v": ao0_v, "ao1_v": ao1_v}))

    def move_logical(self, x: float, y: float) -> dict[str, float]:
        return dict(self.call("move_logical", {"x": x, "y": y}))

    def jog_logical(self, dx: float, dy: float) -> dict[str, float]:
        return dict(self.call("jog_logical", {"dx": dx, "dy": dy}))


def ensure_controller_running(
    dev_name: str,
    ao_x: str,
    ao_y: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    fake_backend: bool = False,
) -> ControllerClient:
    """Start controller service if needed, then return a connected client."""
    client = ControllerClient(host=host, port=port)
    if client.ping():
        return client

    cmd = [
        sys.executable,
        "-m",
        "daq_xy_qt_readback.controller_service",
        "--dev",
        dev_name,
        "--ao-x",
        ao_x,
        "--ao-y",
        ao_y,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if fake_backend:
        cmd.append("--fake-backend")

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        env=_subprocess_env(),
    )

    for _ in range(30):
        time.sleep(0.2)
        if client.ping():
            return client
    raise RuntimeError("Failed to start scanner controller service")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_root = str(Path(__file__).resolve().parents[1])
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_root if not old else src_root + os.pathsep + old
    return env
