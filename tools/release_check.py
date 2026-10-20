"""Verify a wheel installs offline with zero mandatory runtime dependencies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
from zipfile import ZipFile, ZIP_DEFLATED
from time import sleep


class WheelEnvironment(tempfile.TemporaryDirectory):
    def cleanup(self) -> None:
        target = Path(self.name).resolve()
        if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith("agentguard-wheel-"):
            raise RuntimeError("Unexpected temporary environment cleanup path")
        # Windows can briefly keep the just-exited console launcher locked.
        for attempt in range(10):
            try:
                super().cleanup()
                return
            except OSError:
                if attempt == 9:
                    raise
                sleep(.2)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = sorted((root / "dist").glob("agentguard-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one AgentGuard wheel in dist/; use an empty output directory")
    wheel = wheels[0]
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("AGENTGUARD_")}
    environment["PIP_NO_INDEX"] = "1"
    with WheelEnvironment(prefix="agentguard-wheel-") as directory:
        work = Path(directory)
        venv.create(work / "venv", with_pip=True)
        python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        def run(*args: str) -> None:
            subprocess.run([str(python), *args], cwd=work, env=environment, check=True, timeout=60)
        run("-m", "pip", "install", "--no-index", "--no-deps", str(wheel))
        run("-c", "import importlib.metadata as m, importlib.util as u; import agentguard; "
            "assert 'site-packages' in agentguard.__file__; "
            "assert all('extra ==' in req for req in (m.requires('agentguard') or [])); "
            "assert all(u.find_spec(name) is None for name in ('fastapi','redis','yaml','jsonschema','openai','opentelemetry')); "
            "print('Isolated core import and optional dependency isolation: OK')")
        run("-m", "agentguard", "init")
        run("-m", "agentguard", "check")
        run("-m", "agentguard", "simulate")
        cli = python.parent / ("agentguard.exe" if os.name == "nt" else "agentguard")
        subprocess.run([str(cli), "--version"], cwd=work, env=environment, check=True, timeout=15)
    artifacts = [wheel]
    frontend = root / "frontend/dist"
    if (frontend / "index.html").is_file():
        bundle = wheel.parent / "agentguard-console-0.1.0.zip"
        with ZipFile(bundle,"w",compression=ZIP_DEFLATED) as archive:
            for file in sorted(frontend.rglob("*")):
                if file.is_file(): archive.write(file,file.relative_to(frontend).as_posix())
            archive.writestr("agentguard-console.json",json.dumps({"version":"0.1.0","api_version":"1","sdk_version":"0.1.0"}))
        artifacts.append(bundle)
    checksums = "".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n" for file in artifacts)
    (wheel.parent / "SHA256SUMS").write_text(checksums, encoding="ascii")
    print(f"Verified {wheel.name}; SHA256SUMS generated")


if __name__ == "__main__":
    main()
