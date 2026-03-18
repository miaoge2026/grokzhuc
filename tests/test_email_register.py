import json
from pathlib import Path

from email_register import DuckMailConfig, extract_verification_code


def test_extract_verification_code_supports_grok_hyphenated_code():
    content = "Your verification code is ABC-123."
    assert extract_verification_code(content) == "ABC-123"


def test_extract_verification_code_ignores_known_noise_code():
    content = "Subject: Welcome 177010\n<div> 654321 </div>"
    assert extract_verification_code(content) == "654321"


def test_duckmail_config_load_reads_custom_config_path(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "custom.json"
    config_file.write_text(
        json.dumps(
            {
                "duckmail_api_base": "https://example.test",
                "duckmail_bearer": "token-from-file",
                "proxy": "http://proxy.test:8080",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("DUCKMAIL_API_BASE", raising=False)
    monkeypatch.delenv("DUCKMAIL_BEARER", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    config = DuckMailConfig.load(config_file)

    assert config.api_base == "https://example.test"
    assert config.bearer_token == "token-from-file"
    assert config.proxy == "http://proxy.test:8080"


def test_duckmail_config_prefers_environment_variables(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "custom.json"
    config_file.write_text(
        json.dumps(
            {
                "duckmail_api_base": "https://from-file.test",
                "duckmail_bearer": "file-token",
                "proxy": "http://file-proxy.test:8080",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("DUCKMAIL_API_BASE", "https://from-env.test")
    monkeypatch.setenv("DUCKMAIL_BEARER", "env-token")
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy.test:3128")

    config = DuckMailConfig.load(config_file)

    assert config.api_base == "https://from-env.test"
    assert config.bearer_token == "env-token"
    assert config.proxy == "http://env-proxy.test:3128"
