"""Install a wheel in an isolated environment and smoke-test its CLI and web UI."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
import urllib.request
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = scripts / ("cut-agent.exe" if os.name == "nt" else "cut-agent")
        # Windows GitHub runners otherwise choose cp1252 for redirected console
        # output, while the localized CLI help contains Chinese characters.
        child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        subprocess.run([str(python), "-m", "pip", "install", str(args.wheel.resolve())], check=True, env=child_env)
        subprocess.run(
            [str(command), "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            text=True,
            encoding="utf-8",
        )
        server = subprocess.Popen(
            [str(command), "serve", "--port", "8099"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            text=True,
            encoding="utf-8",
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    index = urllib.request.urlopen("http://127.0.0.1:8099/", timeout=2).read().decode()
                    stages = urllib.request.urlopen("http://127.0.0.1:8099/api/stages", timeout=2).read().decode()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("installed service did not start")
                    time.sleep(0.5)
            assert '<div id="root"></div>' in index
            assert "scan_media" in stages
        finally:
            server.terminate()
            server.wait(10)
    print("isolated wheel smoke test passed")


if __name__ == "__main__":
    main()
