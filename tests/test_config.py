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
    "LLM_API_KEY": "sk-test",
    "LLM_MODEL": "claude-sonnet-5-5",
    "BITBUCKET_EMAIL": "rich@example.com",
    "BITBUCKET_API_KEY": "bb-token",
    "BITBUCKET_WORKSPACE": "test-workspace",
}

GITHUB_ENV = {
    "LLM_API_KEY": "sk-test",
    "LLM_MODEL": "claude-sonnet-5-5",
    "GITHUB_TOKEN": "gh-token",
    "GITHUB_OWNER": "octo-org",
}

GITLAB_ENV = {
    "LLM_API_KEY": "sk-test",
    "LLM_MODEL": "claude-sonnet-5-5",
    "GITLAB_TOKEN": "gl-token",
    "GITLAB_URL": "https://git.example.com/gitlab/",
    "GITLAB_NAMESPACE": "engineering/platform",
    "GITLAB_WEBHOOK_SIGNING_TOKEN": "whsec_c2VjcmV0",
}


def test_full_env_loads_with_defaults(monkeypatch, tmp_path):
    # no repos.toml at cwd in the test sandbox — load_settings must not
    # explode, it should just come back with empty registries
    monkeypatch.chdir(tmp_path)
    s = load_settings(FULL_ENV)
    assert s.llm_api_key == "sk-test"
    assert s.llm_model == "claude-sonnet-5-5"
    assert s.llm_base_url is None     # unset = talk to Anthropic directly
    assert s.bitbucket_workspace == "test-workspace"
    assert s.tool_commands == {}
    assert s.repo_checks == {}
    assert s.max_turns == 15
    assert str(s.data_dir).endswith("hone")


def test_llm_base_url_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = load_settings({**FULL_ENV,
                       "LLM_BASE_URL": "https://api.synthetic.new/anthropic"})
    assert s.llm_base_url == "https://api.synthetic.new/anthropic"


def test_missing_required_lists_every_gap():
    # Forges are optional groups now, so the only unconditional requirements
    # are the LLM values; a missing forge is a separate error (below).
    with pytest.raises(ConfigError) as err:
        load_settings({})
    msg = str(err.value)
    assert "LLM_API_KEY" in msg
    assert "LLM_MODEL" in msg
    assert "BITBUCKET_EMAIL" not in msg
    assert "BITBUCKET_API_KEY" not in msg


@pytest.mark.parametrize("env", [GITHUB_ENV, GITLAB_ENV])
def test_single_non_bitbucket_forge_loads(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(env)
    assert settings.bitbucket_email is None
    assert settings.bitbucket_api_key is None
    assert settings.bitbucket_workspace is None


def test_gitlab_settings_and_url_normalization(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(GITLAB_ENV)
    assert settings.gitlab_token == "gl-token"
    assert settings.gitlab_url == "https://git.example.com/gitlab"
    assert settings.gitlab_namespace == "engineering/platform"
    assert settings.gitlab_webhook_signing_token == "whsec_c2VjcmV0"


def test_no_forge_is_a_config_error():
    with pytest.raises(ConfigError, match="no forge configured"):
        load_settings({"LLM_API_KEY": "sk", "LLM_MODEL": "m"})


@pytest.mark.parametrize("partial", [
    {"BITBUCKET_EMAIL": "rich@example.com"},
    {"BITBUCKET_API_KEY": "bb", "BITBUCKET_WORKSPACE": "ws"},
    {"GITHUB_TOKEN": "gh"},
    {"GITLAB_TOKEN": "gl", "GITLAB_URL": "https://gitlab.com"},
    {"GITLAB_NAMESPACE": "engineering"},
])
def test_partial_forge_group_is_a_config_error(partial):
    with pytest.raises(ConfigError, match="set together"):
        load_settings({"LLM_API_KEY": "sk", "LLM_MODEL": "m", **partial})


@pytest.mark.parametrize("url", [
    "ftp://git.example.com",
    "https:///missing-host",
    "https://user:secret@git.example.com",
    "https://git.example.com?x=1",
    "https://git.example.com#fragment",
])
def test_gitlab_url_rejects_credentials_and_malformed_bases(url):
    with pytest.raises(ConfigError, match="GITLAB_URL"):
        load_settings({**GITLAB_ENV, "GITLAB_URL": url})


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
    s = Settings(llm_api_key="k", llm_model="m",
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


def test_github_settings_default_to_unconfigured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = load_settings(FULL_ENV)
    assert s.github_token is None
    assert s.github_owner is None
    assert s.github_webhook_secret is None


def test_github_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = load_settings({**FULL_ENV, "GITHUB_TOKEN": "gh-pat",
                       "GITHUB_OWNER": "technopoetic",
                       "GITHUB_WEBHOOK_SECRET": "gh-s3cr3t"})
    assert s.github_token == "gh-pat"
    assert s.github_owner == "technopoetic"
    assert s.github_webhook_secret == "gh-s3cr3t"


@pytest.mark.parametrize("partial", [
    {"GITHUB_TOKEN": "gh-pat"},
    {"GITHUB_OWNER": "technopoetic"},
])
def test_partial_github_config_is_a_config_error(partial):
    with pytest.raises(ConfigError, match="GITHUB_TOKEN.*GITHUB_OWNER"):
        load_settings({**FULL_ENV, **partial})


def test_configure_logging_writes_to_rotating_file(
        tmp_path, restore_root_logging):
    log_path = configure_logging(tmp_path)
    assert log_path == tmp_path / "logs" / "hone.log"

    logging.getLogger("hone").info("smoke marker from test")

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "smoke marker from test" in content
    assert "hone" in content
