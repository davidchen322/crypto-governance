"""Phase 0 acceptance: repository hygiene.

These run without docker and without network. They encode the Phase 0 exit criteria:
a fresh clone installs, no secrets are tracked, and `.env.example` is complete.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Matches os.getenv("X"), os.environ["X"], os.environ.get("X")
ENV_REF = re.compile(
    r"""os\.(?:getenv\(|environ\.get\(|environ\[)\s*["']([A-Z][A-Z0-9_]*)["']"""
)

SOURCE_DIRS = ("ai_agent", "backend_api", "config", "data_pipeline", "tests")


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def _parse_env_example() -> set[str]:
    keys = set()
    for line in (REPO / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_git_repo_initialized():
    assert (REPO / ".git").exists(), "Phase 0 requires an initialized git repository"


def test_dotenv_is_not_tracked():
    """A .gitignore entry added *after* staging .env does not untrack it."""
    tracked = _tracked_files()
    assert ".env" not in tracked, ".env is tracked by git — secrets would be committed"
    leaked = [f for f in tracked if f.startswith(".env.") and f != ".env.example"]
    assert not leaked, f"env files tracked besides .env.example: {leaked}"


def test_env_example_is_tracked():
    assert ".env.example" in _tracked_files(), ".env.example must be committed"


def test_every_env_var_used_in_code_is_documented():
    """The 'works on my machine' guard: code reads a var that .env.example never mentions."""
    documented = _parse_env_example()
    missing: dict[str, str] = {}
    for directory in SOURCE_DIRS:
        for path in (REPO / directory).rglob("*.py"):
            for key in ENV_REF.findall(path.read_text()):
                if key not in documented:
                    missing[key] = str(path.relative_to(REPO))
    assert not missing, f"env vars read in code but absent from .env.example: {missing}"


def test_compose_env_defaults_are_documented():
    """Every ${VAR} interpolated in docker-compose.yml should appear in .env.example."""
    documented = _parse_env_example()
    compose = (REPO / "docker-compose.yml").read_text()
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}", compose))
    missing = sorted(referenced - documented)
    assert not missing, f"docker-compose.yml references undocumented vars: {missing}"


def test_package_is_importable():
    import config.settings  # noqa: F401
    import data_pipeline  # noqa: F401


def test_settings_load_from_environment(monkeypatch):
    monkeypatch.setenv("POSTGRES_PORT", "65432")
    from config.settings import Settings

    assert Settings.from_env().postgres_port == 65432


@pytest.mark.parametrize(
    "relpath",
    [
        "Makefile",
        "pyproject.toml",
        "docker-compose.yml",
        ".env.example",
        ".gitignore",
        "README.md",
        ".github/workflows/ci.yml",
        "scripts/verify.sh",
        "storage/vector_schema.sql",
    ],
)
def test_required_file_exists(relpath):
    assert (REPO / relpath).exists(), f"missing {relpath}"


def test_verify_script_is_executable():
    assert (REPO / "scripts/verify.sh").stat().st_mode & 0o111, "scripts/verify.sh is not +x"


def test_pyproject_declares_integration_markers():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    names = {m.split(":", 1)[0] for m in markers}
    assert {"integration", "seed", "persistence"} <= names
