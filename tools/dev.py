"""Start the local backend and Vite together; Ctrl+C closes both processes."""
from __future__ import annotations

import argparse
from ipaddress import ip_address
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from time import sleep


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Frontend loopback or explicit trusted LAN IPv4")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--database", help="Use an explicit console database without enabling demo data")
    parser.add_argument("--llm-upstream-url", help="OpenAI-compatible provider URL, for example https://api.openai.com/v1")
    args = parser.parse_args()
    try:
        host = ip_address(args.host)
        if host.version != 4 or host.is_unspecified or host.is_multicast:
            raise ValueError()
    except ValueError:
        parser.error("Use an explicit local IPv4 address")
    root = Path(__file__).resolve().parents[1]
    vite = root / "frontend/node_modules/vite/bin/vite.js"
    node = shutil.which("node")
    if node is None or not vite.is_file():
        parser.error("Install Node.js 22.12+ and run npm ci in frontend first")
    for address, port in (("127.0.0.1",8000),(str(host),5173)):
        try:
            with socket.socket() as check: check.bind((address,port))
        except OSError:
            parser.error(f"{address}:{port} is unavailable; stop the existing service first")
    backend = [sys.executable,"-m","agentguard.api.http","--allow-origin",f"http://{host}:5173"]
    if args.demo: backend.append("--demo")
    if args.database: backend.extend(["--database", args.database])
    if args.llm_upstream_url: backend.extend(["--llm-upstream-url", args.llm_upstream_url])
    commands = [(backend,root),([node,str(vite),"--host",str(host)],root / "frontend")]
    processes = []
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        for command, directory in commands:
            processes.append(subprocess.Popen(command,cwd=directory,creationflags=flags))
        print(f"Console: http://{host}:5173/ | API docs: http://{host}:5173/docs | Ctrl+C to stop",flush=True)
        while all(process.poll() is None for process in processes):
            sleep(.5)
        return next((process.returncode for process in processes if process.returncode),1)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
