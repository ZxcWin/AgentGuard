"""Create an exclusive credential file and private token file for one workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".agentguard/credentials.json")
    args = parser.parse_args()
    path = Path(args.output)
    token_path = path.with_suffix(".tokens.json")
    if path.exists() or token_path.exists():
        raise SystemExit("Output exists; use a new path to rotate credentials")
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {role: secrets.token_urlsafe(32) for role in ("viewer", "operator", "admin")}
    rows = [{"subject": role, "role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for role, token in tokens.items()]
    for target, content in ((path, rows), (token_path, tokens)):
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(content, stream, indent=2)
    print(f"Credentials created: {path}; private tokens: {token_path}")
    print("Protect these files with user-only ACLs on Windows; do not commit or expose the token file")


if __name__ == "__main__":
    main()
