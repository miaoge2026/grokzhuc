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
                "duckmail_api_base": "https://duckmail.test",
                "duckmail_bearer": "duckmail-token",
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
        "GROK2API_ENDPOINT",
        "GROK2API_TOKEN",
        "GROK2API_APPEND",
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
