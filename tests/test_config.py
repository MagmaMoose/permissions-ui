import pytest

from permissions_ui.config import ConfigError, load_config


def test_the_test_environment_loads(env):
    cfg = load_config(env)
    assert cfg.github.repo == "example/access" and cfg.github.root == "access"
    assert cfg.group_prefixes == ("acl-", "k8s-") and cfg.auto_merge and cfg.fulfilment == "pull-request"
    assert cfg.redirect_uri == "https://access.example.test/auth/callback"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"AZURE_TENANT_ID": "nope"}, "AZURE_TENANT_ID must be a GUID"),
        ({"PERMISSIONS_UI_SESSION_KEY": "short"}, "at least 32 characters"),
        ({"PERMISSIONS_UI_PUBLIC_ORIGIN": "http://access.example.test"}, "must be https"),  # DevSkim: ignore DS137138 - deliberately invalid input; test asserts config rejects it
        ({"PERMISSIONS_UI_PUBLIC_ORIGIN": "https://access.example.test/path"}, "with no path"),
        ({"PERMISSIONS_UI_GITHUB_REPO": "not a repo"}, "owner/name"),
        ({"PERMISSIONS_UI_FULFILMENT": "magic"}, "takes pull-request or graph"),
        ({"PERMISSIONS_UI_MERGE": "sometimes"}, "takes auto or manual"),
        ({"PERMISSIONS_UI_FULFILMENT": "graph", "PERMISSIONS_UI_MERGE": "manual"}, "needs PERMISSIONS_UI_MERGE=auto"),
        ({"PERMISSIONS_UI_CATALOG_PATH": "../escape"}, "without .."),
        ({"PERMISSIONS_UI_GITHUB_API": "http://api.github.test"}, "must be https"),  # DevSkim: ignore DS137138 - deliberately invalid input; test asserts config rejects it
        ({"PERMISSIONS_UI_TEAMS_WEBHOOK": "http://hooks.example.test"}, "must be an https URL"),  # DevSkim: ignore DS137138 - deliberately invalid input; test asserts config rejects it
        ({"PERMISSIONS_UI_SESSION_HOURS": "48"}, "between 1 and 24"),
        ({"PERMISSIONS_UI_BRAND_ACCENT": "teal"}, "#rrggbb"),
        ({"PERMISSIONS_UI_ADMIN_ROLE": "has spaces"}, "app role value"),
    ],
)
def test_bad_values_refuse_to_start(env, change, message):
    with pytest.raises(ConfigError, match=message):
        load_config({**env, **change})


def test_a_github_app_needs_its_key_and_a_numeric_id(env):
    base = {k: v for k, v in env.items() if k != "PERMISSIONS_UI_GITHUB_TOKEN"}
    with pytest.raises(ConfigError, match="needs both"):
        load_config({**base, "PERMISSIONS_UI_GITHUB_APP_ID": "123"})
    with pytest.raises(ConfigError, match="numeric id"):
        load_config({**base, "PERMISSIONS_UI_GITHUB_APP_ID": "abc", "PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY": "pem"})
    with pytest.raises(ConfigError, match="preferred"):
        load_config(base)
    assert load_config({**base, "PERMISSIONS_UI_GITHUB_APP_ID": "123", "PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY": "pem"}).github.app_id == "123"


def test_one_catalog_source(env, tmp_path):
    with pytest.raises(ConfigError, match="not both"):
        load_config({**env, "PERMISSIONS_UI_CATALOG_DIR": str(tmp_path)})
    local = {k: v for k, v in env.items() if not k.startswith("PERMISSIONS_UI_GITHUB")}
    with pytest.raises(ConfigError, match="not a directory"):
        load_config({**local, "PERMISSIONS_UI_CATALOG_DIR": str(tmp_path / "missing")})
    assert load_config({**local, "PERMISSIONS_UI_CATALOG_DIR": str(tmp_path)}).github is None


def test_secrets_come_from_files(env, tmp_path):
    key = tmp_path / "session"
    key.write_text("s" * 40)
    cfg = load_config({**{k: v for k, v in env.items() if k != "PERMISSIONS_UI_SESSION_KEY"}, "PERMISSIONS_UI_SESSION_KEY_FILE": str(key)})
    assert cfg.session_key == b"s" * 40
    with pytest.raises(ConfigError, match="cannot read"):
        load_config({**{k: v for k, v in env.items() if k != "PERMISSIONS_UI_SESSION_KEY"}, "PERMISSIONS_UI_SESSION_KEY_FILE": str(tmp_path / "nope")})
    no_credential = {k: v for k, v in env.items() if k != "AZURE_FEDERATED_TOKEN_FILE"}
    with pytest.raises(ConfigError, match="AZURE_FEDERATED_TOKEN_FILE"):
        load_config(no_credential)
