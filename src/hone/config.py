"""Environment-driven configuration. Missing secrets are a startup error,
not something to discover mid-review."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("hone")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    synthetic_api_key: str
    synthetic_model: str
    bitbucket_email: str
    bitbucket_username: str  # git-over-HTTPS needs username, not email
    bitbucket_api_key: str
    bitbucket_workspace: str
    data_dir: Path
    # Tool registry (name -> argv) from repos.toml. A tool with no `command`
    # (value is None) is a builtin resolved in tools.py — see eslint, which
    # must never take its command from repo-supplied config.
    tool_commands: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    # repo slug -> enabled tool names. A repo with no entry gets no checks —
    # unknown repos default to safe (nothing), not to Mekanik's Python tools.
    repo_checks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_turns: int = 15
    # Optional at load time — `serve` refuses to start without setting it
    # itself, keeping the CLI usable without a webhook secret.
    webhook_secret: str | None = None
    port: int = 8420


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from a mapping (tests inject; production passes None
    to read os.environ). Raises ConfigError listing every missing var."""
    source = os.environ if env is None else env

    required = {
        "SYNTHETIC_API_KEY": source.get("SYNTHETIC_API_KEY"),
        "BITBUCKET_EMAIL": source.get("BITBUCKET_EMAIL"),
        "BITBUCKET_API_KEY": source.get("BITBUCKET_API_KEY"),
        "BITBUCKET_WORKSPACE": source.get("BITBUCKET_WORKSPACE"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigError("Missing required env vars: " + ", ".join(missing))

    repo_config_path = Path(source.get("HONE_REPO_CONFIG", "repos.toml"))
    tool_commands, repo_checks = load_repo_config(repo_config_path)

    return Settings(
        synthetic_api_key=required["SYNTHETIC_API_KEY"],
        synthetic_model=source.get("SYNTHETIC_MODEL", "syn:large:text"),
        bitbucket_email=required["BITBUCKET_EMAIL"],
        bitbucket_username=(source.get("BITBUCKET_USERNAME")
                            or required["BITBUCKET_EMAIL"]),
        bitbucket_api_key=required["BITBUCKET_API_KEY"],
        bitbucket_workspace=required["BITBUCKET_WORKSPACE"],
        data_dir=Path(
            source.get("HONE_DATA_DIR", str(Path.home() / ".local/share/hone"))
        ).expanduser(),
        tool_commands=tool_commands,
        repo_checks=repo_checks,
        max_turns=int(source.get("HONE_MAX_TURNS", "15")),
        webhook_secret=source.get("HONE_WEBHOOK_SECRET") or None,
        port=int(source.get("HONE_PORT", "8420")),
    )


def load_repo_config(
    path: Path,
) -> tuple[dict[str, tuple[str, ...] | None], dict[str, tuple[str, ...]]]:
    """Parse repos.toml: a tool registry (name -> argv, or no `command` for
    a builtin) plus per-repo enablement by tool name. A missing file is not
    an error — it means no repo gets any static check, which is the safe
    default (silently running Mekanik's ruff/mypy against an unrelated repo
    would be worse than running nothing)."""
    if not path.is_file():
        return {}, {}

    with path.open("rb") as f:
        data = tomllib.load(f)

    tools: dict[str, tuple[str, ...] | None] = {
        name: tuple(cfg["command"]) if cfg and "command" in cfg else None
        for name, cfg in (data.get("tools") or {}).items()
    }
    repos: dict[str, tuple[str, ...]] = {
        name: tuple(checks) for name, checks in (data.get("repos") or {}).items()
    }

    for repo, checks in repos.items():
        for name in checks:
            if name not in tools:
                log.warning(
                    "repos.toml: repo %r enables unknown tool %r — it will "
                    "error if the agent calls it", repo, name)

    return tools, repos


def resolve_static_checks(settings: Settings, repo: str
                          ) -> dict[str, tuple[str, ...] | None]:
    """The tool-name -> argv mapping for one repo, ready for ToolBox. A
    repo not listed in repos.toml gets no checks."""
    return {name: settings.tool_commands.get(name)
            for name in settings.repo_checks.get(repo, ())}


def configure_logging(data_dir: Path) -> Path:
    """INFO to stderr and to data_dir/logs/hone.log (rotating, 3x1 MB).

    Returns the log file path. Handlers live on the root logger so child
    loggers (hone, httpx, ...) propagate to both sinks by default — no
    per-logger handler wiring needed. Call once at process start."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "hone.log"

    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    file_handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)
    return log_path
