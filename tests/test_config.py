import logging
from pathlib import Path

import pytest

from hone.config import (
    ConfigError,
    Settings,
    configure_logging,
    load_repo_config,
    load_settings,
    resolve_static_checks,
)


@pytest.fixture
def restore_root_logging():
    """Root logger is global — any handler/level we add must be unwound so
    later tests don't get duplicate output into this test's tmp file."""
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    level_before = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level_before)


FULL_ENV = {
    "SYNTHETIC_API_KEY": "sk-syn-test",
    "BITBUCKET_EMAIL": "rich@example.com",
    "BITBUCKET_API_KEY": "bb-token",
    "BITBUCKET_WORKSPACE": "test-workspace",
}


def test_full_env_loads_with_defaults(monkeypatch, tmp_path):
    # no repos.toml at cwd in the test sandbox — load_settings must not
    # explode, it should just come back with empty registries
    monkeypatch.chdir(tmp_path)
    s = load_settings(FULL_ENV)
    assert s.synthetic_api_key == "sk-syn-test"
    assert s.synthetic_model == "syn:large:text"
    assert s.bitbucket_workspace == "test-workspace"
    assert s.tool_commands == {}
    assert s.repo_checks == {}
    assert s.max_turns == 15
    assert str(s.data_dir).endswith("hone")


def test_missing_required_lists_every_gap():
    with pytest.raises(ConfigError) as err:
        load_settings({})
    msg = str(err.value)
    assert "SYNTHETIC_API_KEY" in msg
    assert "BITBUCKET_EMAIL" in msg
    assert "BITBUCKET_API_KEY" in msg


def test_missing_repo_config_file_is_not_an_error(tmp_path):
    tools, repos = load_repo_config(tmp_path / "no-such-file.toml")
    assert tools == {}
    assert repos == {}


def test_repo_config_parses_tools_and_repos(tmp_path):
    path = tmp_path / "repos.toml"
    path.write_text("""
[tools.ruff]
command = ["ruff", "check", "."]

[tools.eslint]

[repos]
mekanik = ["ruff"]
"mekanik-vue" = ["eslint"]
""")
    tools, repos = load_repo_config(path)
    assert tools == {"ruff": ("ruff", "check", "."), "eslint": None}
    assert repos == {"mekanik": ("ruff",), "mekanik-vue": ("eslint",)}


def test_repo_config_warns_on_unknown_tool(tmp_path, caplog):
    path = tmp_path / "repos.toml"
    path.write_text('[repos]\nfoo = ["bogus"]\n')
    with caplog.at_level(logging.WARNING):
        load_repo_config(path)
    assert "bogus" in caplog.text


def test_resolve_static_checks_scopes_to_one_repo():
    s = Settings(synthetic_api_key="k", synthetic_model="m",
                bitbucket_email="e", bitbucket_username="u",
                bitbucket_api_key="k", bitbucket_workspace="ws",
                data_dir=Path("/tmp"),
                tool_commands={"ruff": ("ruff", "check", "."),
                              "eslint": None},
                repo_checks={"mekanik": ("ruff",),
                            "mekanik-vue": ("eslint",)})
    assert resolve_static_checks(s, "mekanik") == {
        "ruff": ("ruff", "check", ".")}
    assert resolve_static_checks(s, "mekanik-vue") == {"eslint": None}
    assert resolve_static_checks(s, "unlisted-repo") == {}


def test_webhook_settings_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = load_settings(FULL_ENV)
    assert s.webhook_secret is None          # optional until serve runs
    assert s.port == 8420


def test_webhook_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = load_settings({**FULL_ENV, "HONE_WEBHOOK_SECRET": "s3cr3t",
                       "HONE_PORT": "9000"})
    assert s.webhook_secret == "s3cr3t"
    assert s.port == 9000


def test_configure_logging_writes_to_rotating_file(
        tmp_path, restore_root_logging):
    log_path = configure_logging(tmp_path)
    assert log_path == tmp_path / "logs" / "hone.log"

    logging.getLogger("hone").info("smoke marker from test")

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "smoke marker from test" in content
    assert "hone" in content
