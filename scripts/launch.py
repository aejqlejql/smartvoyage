"""Local demo launcher; does not install dependencies, reset data or start supplier services."""

import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from ..infrastructure.config import _load_local_env


def main():
    _load_local_env()
    missing = [name for name in ("fastapi", "pydantic", "uvicorn") if importlib.util.find_spec(name) is None]
    if missing:
        print("Missing dependencies:", ", ".join(missing))
        print("Run: python -m pip install -r ai01_SmartVoyage/requirements-demo.txt")
        return 1
    root = Path(__file__).resolve().parents[2]
    port = int(os.getenv("SMARTVOYAGE_API_PORT", "8080"))
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            print(f"Port {port} is already occupied. No process was stopped.")
            print("Set SMARTVOYAGE_API_PORT to an available port and retry.")
            return 1
    env = {**os.environ, "SMARTVOYAGE_MODE": "demo", "SMARTVOYAGE_API_HOST": "127.0.0.1"}
    child = subprocess.Popen([sys.executable, "-m", "ai01_SmartVoyage.api.server"], cwd=root, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            if child.poll() is not None:
                return child.returncode
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=0.5) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        print(f"SmartVoyage demo: {url} (Ctrl+C to stop)")
                        break
            except OSError:
                time.sleep(0.25)
        return child.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
