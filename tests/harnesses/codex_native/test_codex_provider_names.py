"""Session-config provider inventory used by the model-switch path.

A web-UI model switch sends ``thread/settings/update`` with a ``modelProvider``.
Codex validates that name against the providers defined in the session's
private config and rejects anything else with ``-32600``:

    invalid thread settings override: invalid value for `model_provider`:
    `omnigent_provider` is not in the allowed set a configured model provider

``read_codex_home_config_provider_names`` supplies the set to validate
against, so Omnigent never replays a remembered provider name that the
session config no longer defines.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native.bridge import (
    read_codex_home_config_provider_names,
    read_codex_home_config_model_provider,
)


def _home(tmp_path: Path, body: str) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(body, encoding="utf-8")
    return home


def test_reads_defined_provider_names(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        'model_provider = "openrouter"\n'
        "\n"
        "[model_providers.multidex]\n"
        'base_url = "http://local/v1"\n'
        "\n"
        "[model_providers.openrouter]\n"
        'base_url = "https://openrouter.ai/api/v1"\n',
    )

    assert read_codex_home_config_provider_names(home) == frozenset(
        {"multidex", "openrouter"}
    )


def test_inline_dotted_provider_is_detected(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        'model_provider = "omnigent_provider"\n'
        "\n"
        "[model_providers]\n"
        'omnigent_provider = {name = "Omnigent Provider", base_url = "https://x/v1"}\n',
    )

    assert read_codex_home_config_provider_names(home) == frozenset({"omnigent_provider"})


def test_missing_table_is_absent_from_result(tmp_path: Path) -> None:
    # The exact failure shape: a local-model session that never received the
    # omnigent_provider table.
    home = _home(
        tmp_path,
        'model_provider = "openrouter"\nmodel = "deepseek/deepseek-v4.1-flash-1017"\n'
        "\n[model_providers.openrouter]\nbase_url = \"https://openrouter.ai/api/v1\"\n",
    )

    names = read_codex_home_config_provider_names(home)
    assert "omnigent_provider" not in names
    assert read_codex_home_config_model_provider(home) == "openrouter"


def test_unreadable_or_providerless_config_returns_empty(tmp_path: Path) -> None:
    assert read_codex_home_config_provider_names(tmp_path / "absent") == frozenset()
    assert read_codex_home_config_provider_names(_home(tmp_path, 'model = "gpt-6-astra"\n')) == frozenset()
