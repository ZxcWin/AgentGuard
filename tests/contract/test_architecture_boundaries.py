import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "agentguard"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_domain_does_not_depend_on_outer_layers_or_optional_packages():
    forbidden_prefixes = (
        "agentguard.api",
        "agentguard.application",
        "agentguard.adapters",
        "agentguard.config",
        "agentguard.policies",
        "openai",
        "anthropic",
        "redis",
        "langchain",
    )

    for path in (PACKAGE_ROOT / "domain").glob("*.py"):
        for module in imported_modules(path):
            assert not module.startswith(forbidden_prefixes), f"{path} imports {module}"


def test_core_package_has_no_required_provider_imports():
    forbidden = ("openai", "anthropic", "langchain", "llama_index", "redis")

    for path in PACKAGE_ROOT.rglob("*.py"):
        for module in imported_modules(path):
            assert not module.startswith(forbidden), f"{path} imports {module}"
