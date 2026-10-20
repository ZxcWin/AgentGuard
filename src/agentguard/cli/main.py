"""Offline developer commands. No provider clients or tools are loaded."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..application.runtime import AgentGuard
from ..config.manager import ConfigManager
from ..config.schema import AgentGuardConfig
from ..config.loader import load_yaml_mapping
from ..domain.exceptions import AgentGuardError, ConfigurationError


class InputError(ValueError):
    pass


def read_json(path: str) -> Any:
    with Path(path).open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise InputError("Input exceeds 1 MiB")
    return json.loads(data.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(InputError("Invalid JSON number")))


def configuration(path: str) -> AgentGuardConfig:
    if Path(path).suffix.lower() in {".yml", ".yaml"}:
        try:
            values = load_yaml_mapping(path)
        except Exception:
            raise InputError("Invalid YAML configuration or missing YAML dependency") from None
    else:
        values = read_json(path)
    return AgentGuardConfig.from_mapping(values)


def fixture_record(value: Any) -> dict[str, Any]:
    allowed = {"model", "messages", "max_tokens", "response"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise InputError("Fixture must contain only model, messages, max_tokens, response")
    result = {"model": "local-example", "messages": "local offline example", "max_tokens": 32,
              "response": {"content": "{}", "usage": {"input_tokens": 4, "output_tokens": 8}}, **value}
    if not isinstance(result["model"], str) or not 1 <= len(result["model"]) <= 100:
        raise InputError("Fixture model is invalid")
    if not isinstance(result["messages"], str) or len(result["messages"]) > 200000:
        raise InputError("Fixture messages must be bounded text")
    if type(result["max_tokens"]) is not int or not 1 <= result["max_tokens"] <= 1000000:
        raise InputError("Fixture max_tokens is invalid")
    if not isinstance(result["response"], (dict, str)) or len(json.dumps(result["response"])) > 200000:
        raise InputError("Fixture response must be bounded JSON object or text")
    return result


def simulate(config: AgentGuardConfig, records: list[dict[str, Any]], *, command: str) -> tuple[dict[str, Any], int]:
    if config.rate_limit.backend != "memory":
        raise InputError("Offline simulation requires rate_limit.backend=memory")
    guard = AgentGuard(config_manager=ConfigManager(config, environ={}))
    outcomes = []
    run = guard.run(name=f"offline-{command}")
    try:
        with run:
            for index, row in enumerate(records):
                response = row["response"]
                def provider(**kwargs):
                    # Return a fresh object for retries; fixtures never execute code.
                    return json.loads(json.dumps(response))
                try:
                    run.invoke(provider, model=row["model"], messages=row["messages"], max_tokens=row["max_tokens"])
                except AgentGuardError as error:
                    outcomes.append({"index": index, "allowed": False, "reason": type(error).__name__})
                else:
                    outcomes.append({"index": index, "allowed": True})
    finally:
        guard.close()
    # A run can complete after the caller catches a rejection; decisions remain explicit.
    events = [{key: event[key] for key in ("event", "schema_version", "reason_code", "action", "attempt") if key in event}
              for event in run.state.events]
    rejected = any(not row["allowed"] for row in outcomes)
    return {"command": command, "offline": True, "shadow": command == "replay", "decisions": outcomes,
            "summary": run.state.summary(), "events": events}, int(rejected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentguard", description="AgentGuard offline configuration and policy tools")
    parser.add_argument("--version", action="version", version="agentguard 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a development JSON configuration without overwriting files")
    init.add_argument("--output", default="agentguard.json")
    drain = sub.add_parser("audit-deliver", help="Deliver a bounded batch to a durable deduplicating SQLite archive")
    drain.add_argument("--source", required=True, help="Existing audit outbox database")
    drain.add_argument("--archive", required=True, help="Separate durable archive database")
    drain.add_argument("--limit", type=int, default=100, choices=range(1,1001), metavar="1..1000")
    for name in ("check", "explain", "simulate", "replay"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", default="agentguard.json")
        if name == "replay":
            cmd.add_argument("fixture", help="JSON array of offline request/response fixtures; shadow only")
        elif name != "check":
            cmd.add_argument("--fixture", help="JSON object containing an offline request/response fixture")
    args = parser.parse_args(argv)
    try:
        if args.command == "audit-deliver":
            from ..adapters.storage.audit import SQLiteAuditStore
            from ..adapters.storage.audit_archive import SQLiteAuditArchive
            if not Path(args.source).is_file() or Path(args.source).resolve() == Path(args.archive).resolve():
                raise InputError("Source must exist and archive must be separate")
            source = SQLiteAuditStore(args.source)
            try:
                archive = SQLiteAuditArchive(args.archive)
                try:
                    result = source.deliver(archive, limit=args.limit)
                    result["pending"] = source.stats()["pending"]
                    result["pruned_archive"] = archive.prune()
                    result["command"] = "audit-deliver"
                    status = 0
                finally:
                    archive.close()
            finally:
                source.close()
        elif args.command == "init":
            config = AgentGuardConfig.from_mapping({"token": {"max_output_tokens": 256, "run_token_budget": 32000},
                "security": {"enabled": True}, "budget": {"run_cost_usd": 1},
                "pricing": {"models": {"local-example": {"input_per_million": 0, "output_per_million": 0}}}})
            with Path(args.output).open("x", encoding="utf-8") as stream:
                json.dump(asdict(config), stream, indent=2, ensure_ascii=True)
                stream.write("\n")
            result, status = {"created": True, "profile": "development", "note": "local-example prices are synthetic; configure real model prices before use"}, 0
        else:
            config = configuration(args.config)
            if args.command == "check":
                dependencies = {"yaml": importlib.util.find_spec("yaml") is not None,
                                "redis": importlib.util.find_spec("redis") is not None,
                                "quality": importlib.util.find_spec("jsonschema") is not None}
                if config.rate_limit.backend == "redis" and not dependencies["redis"]:
                    raise ConfigurationError("Redis optional dependency is missing")
                result, status = {"valid": True, "schema_version": config.schema_version,
                                  "profile": config.enforcement.profile, "dependencies": dependencies}, 0
            else:
                raw = read_json(args.fixture) if args.fixture else {}
                if args.command == "replay":
                    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
                        raise InputError("Replay requires 1 to 100 offline fixtures")
                    rows = [fixture_record(row) for row in raw]
                else:
                    rows = [fixture_record(raw)]
                result, status = simulate(config, rows, command=args.command)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, sqlite3.Error, AgentGuardError):
        # Do not print parser/provider/config error values containing secrets.
        result, status = {"error": {"code": "invalid_input_or_config", "message": "Check file access, JSON/YAML fields, size limits and optional dependencies; init never overwrites files"}}, 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
