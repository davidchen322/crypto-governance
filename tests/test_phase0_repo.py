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

# Matches environment reads with a literal key: getenv, environ.get, environ[...]
ENV_REF = re.compile(r"""os\.(?:getenv\(|environ\.get\(|environ\[)\s*["']([A-Z][A-Z0-9_]*)["']""")

# Application code only. Tests carry their own local defaults and are not part of the
# contract that .env.example documents. `dags` joined Phase 7 — DAG code reads env vars
# (SPARK_CONTAINER_NAME) exactly like every other application directory here.
SOURCE_DIRS = ("ai_agent", "backend_api", "config", "data_pipeline", "dags")


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
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


# --------------------------------------------------------------------------
# Harness isolation — verify.sh destroys volumes, so it must never target the dev stack
# --------------------------------------------------------------------------


def _verify_script() -> str:
    return (REPO / "scripts/verify.sh").read_text()


def test_verify_runs_under_its_own_compose_project():
    """`verify.sh` opens with `docker compose down -v`. Without an isolated project name
    that wipes harvested bronze data — hours of re-fetching from rate-limited public APIs
    because someone ran the test suite."""
    script = _verify_script()
    assert "COMPOSE_PROJECT_NAME=" in script, "verify.sh does not set a Compose project"
    project = re.search(r"COMPOSE_PROJECT_NAME=(\S+)", script).group(1)
    default = re.search(r"^name:\s*(\S+)", (REPO / "docker-compose.yml").read_text(), re.M).group(1)
    assert project != default, (
        f"verify.sh shares the project name {default!r} with the dev stack; "
        "its `down -v` would destroy real data"
    )


def test_verify_uses_ports_that_do_not_collide_with_the_dev_stack():
    """Both stacks must be able to run at once, or the harness cannot be used while
    developing."""
    script = _verify_script()
    defaults = dict(
        line.split("=", 1)
        for line in (REPO / ".env.example").read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    )
    for var in ("POSTGRES_PORT", "MINIO_PORT", "ICEBERG_REST_PORT"):
        match = re.search(rf"^export {var}=(\d+)", script, re.M)
        assert match, f"verify.sh does not override {var}"
        assert match.group(1) != defaults[var].strip(), (
            f"verify.sh reuses the dev {var} ({defaults[var].strip()}); "
            "the two stacks cannot run simultaneously"
        )


def test_compose_pins_no_container_names():
    """A pinned container_name is global, so two projects using this file would collide."""
    compose = (REPO / "docker-compose.yml").read_text()
    pinned = re.findall(r"^\s*container_name:\s*(\S+)", compose, re.M)
    assert not pinned, f"container_name blocks running a second project: {pinned}"


def test_every_third_party_import_is_a_declared_dependency():
    """The 'works on my machine' guard for packages rather than env vars.

    Phase 5 was written against langgraph installed by hand into the local venv; it was
    absent from pyproject.toml for the whole build, so a fresh clone would have failed at
    import with nothing in the repo explaining why. `pip install -e .` succeeding locally
    proves nothing when the local venv already has the package."""
    import tomllib

    declared = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    names = {d.split(">")[0].split("=")[0].split("[")[0].strip().lower() for d in declared}
    # Distribution name -> the module it provides, where they differ.
    provides = {"psycopg[binary]": "psycopg", "pyyaml": "yaml", "langgraph": "langgraph"}
    names |= {v for k, v in provides.items() if k.split("[")[0] in names}

    third_party = {
        "boto3",
        "psycopg",
        "requests",
        "yaml",
        "tiktoken",
        "langgraph",
        "langchain_core",
    }
    used: set[str] = set()
    for directory in SOURCE_DIRS:
        for path in (REPO / directory).rglob("*.py"):
            text = path.read_text()
            for module in third_party:
                if f"import {module}" in text or f"from {module}" in text:
                    used.add(module)

    # langchain_core arrives as a langgraph dependency; declaring langgraph covers it.
    covered = names | ({"langchain_core"} if "langgraph" in names else set())
    missing = sorted(used - covered)
    assert not missing, f"imported in code but not declared in pyproject.toml: {missing}"
