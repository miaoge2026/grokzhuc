import json
from pathlib import Path

from DrissionPage_example import AppConfig


def test_app_config_load_uses_custom_config_file(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "run": {"count": 42},
                "browser_proxy": "http://proxy.test:7890",
                "headless": False,
                "output_dir": "custom-output",
                "user_data_dir": "custom-user-data",
                "chromium_path": "/opt/chrome/chrome",
                "duckmail_api_base": "https://duckmail.test",
                "duckmail_bearer": "duckmail-token",
                "timeouts": {"email": 30, "code": 240, "profile": 150, "sso": 200, "page": 90},
                "retry": {"max_retries": 5, "delay": 3.5},
                "api": {"endpoint": "https://api.test", "token": "secret", "append": False},
            }
        ),
        encoding="utf-8",
    )

    for name in [
        "RUN_COUNT",
        "LOG_LEVEL",
        "BROWSER_PROXY",
        "HEADLESS",
        "CHROMIUM_PATH",
        "GROK2API_ENDPOINT",
        "GROK2API_TOKEN",
        "GROK2API_APPEND",
        "OUTPUT_DIR",
        "USER_DATA_DIR",
        "EMAIL_TIMEOUT",
        "CODE_TIMEOUT",
        "PROFILE_TIMEOUT",
        "SSO_TIMEOUT",
        "PAGE_TIMEOUT",
        "MAX_RETRIES",
        "RETRY_DELAY",
        "DUCKMAIL_API_BASE",
        "DUCKMAIL_BEARER",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ]:
        monkeypatch.delenv(name, raising=False)

    config = AppConfig.load(str(config_file))

    assert config.run_count == 42
    assert config.browser_proxy == "http://proxy.test:7890"
    assert config.headless is False
    assert config.output_dir == "custom-output"
    assert config.user_data_dir == "custom-user-data"
    assert config.chromium_path == "/opt/chrome/chrome"
    assert config.email_timeout == 30
    assert config.code_timeout == 240
    assert config.profile_timeout == 150
    assert config.sso_timeout == 200
    assert config.page_timeout == 90
    assert config.max_retries == 5
    assert config.retry_delay == 3.5
    assert config.api_endpoint == "https://api.test"
    assert config.api_token == "secret"
    assert config.api_append is False
    assert config.duckmail_config.api_base == "https://duckmail.test"
    assert config.duckmail_config.bearer_token == "duckmail-token"


def test_app_config_load_falls_back_when_env_values_are_invalid(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"run": {"count": 7}, "headless": True}), encoding="utf-8")

    monkeypatch.setenv("RUN_COUNT", "invalid")
    monkeypatch.setenv("HEADLESS", "maybe")

    config = AppConfig.load(str(config_file))

    assert config.run_count == 7
    assert config.headless is True


def test_app_config_load_prefers_environment_for_extended_runtime_settings(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "output_dir": "from-file-output",
                "user_data_dir": "from-file-user-data",
                "chromium_path": "/from/file/chrome",
                "timeouts": {"email": 15, "code": 180, "profile": 120, "sso": 120, "page": 60},
                "retry": {"max_retries": 3, "delay": 2.0},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUTPUT_DIR", "from-env-output")
    monkeypatch.setenv("USER_DATA_DIR", "from-env-user-data")
    monkeypatch.setenv("CHROMIUM_PATH", "/from/env/chrome")
    monkeypatch.setenv("EMAIL_TIMEOUT", "25")
    monkeypatch.setenv("CODE_TIMEOUT", "205")
    monkeypatch.setenv("PROFILE_TIMEOUT", "135")
    monkeypatch.setenv("SSO_TIMEOUT", "145")
    monkeypatch.setenv("PAGE_TIMEOUT", "75")
    monkeypatch.setenv("MAX_RETRIES", "6")
    monkeypatch.setenv("RETRY_DELAY", "4.5")

    config = AppConfig.load(str(config_file))

    assert config.output_dir == "from-env-output"
    assert config.user_data_dir == "from-env-user-data"
    assert config.chromium_path == "/from/env/chrome"
    assert config.email_timeout == 25
    assert config.code_timeout == 205
    assert config.profile_timeout == 135
    assert config.sso_timeout == 145
    assert config.page_timeout == 75
    assert config.max_retries == 6
    assert config.retry_delay == 4.5
