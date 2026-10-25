from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from omnigent.harnesses.localdex_native.config import (
    load_localdex_config,
    localdex_model_for_selection,
    localdex_provider_config_overrides,
    localdex_runtime_provider_id,
    with_localdex_model_picker_row,
)


@pytest.mark.parametrize("explicit_fast", [False, True])
def test_lithos_provider_registers_three_variants_with_one_model_label(
    tmp_path: Path, explicit_fast: bool
) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        '[model_providers.lithos]\nenv_key = "LITHOS_API_KEY"\n', encoding="utf-8"
    )
    models_path = tmp_path / "models.toml"
    if explicit_fast:
        models_path.write_text(
            '[models."deepseek-ai/DeepSeek-V4.1-Flash-fast"]\nprovider = "lithos"\n',
            encoding="utf-8",
        )
    registration = load_localdex_config(providers, models_path=models_path, require_token=False)
    rows = with_localdex_model_picker_row([], registration)
    assert {row["modelFamily"] for row in rows} == {"ds-v4.1-flash-1017"}
    assert {row["displayName"] for row in rows} == {"DeepSeek v4.1 Flash"}
    assert {row["providerId"] for row in rows} == {"lithos"}
    assert {row["variant"] for row in rows} == {"regular", "fast", "ultra"}
    assert all(row["defaultReasoningEffort"] == "max" for row in rows)
    assert next(row for row in rows if row["variant"] == "regular")["id"] == (
        "deepseek-ai/DeepSeek-V4.1-Flash"
    )
    native = tomllib.loads(localdex_provider_config_overrides(registration)[0])["model_providers"]
    assert "lithos" in native
    assert "name" not in native["lithos"]
    assert native["lithos"]["base_url"] == "https://api.lithosai.cloud/v1"
    assert native["lithos"]["env_key"] == "LITHOS_API_KEY"


@pytest.mark.parametrize(
    "provider_fields",
    [
        "",
        'base_url = "https://openrouter.ai/api/v1"\nenv_key = "OPENROUTER_API_KEY"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nname = "OpenRouter"\n',
    ],
)
def test_openrouter_registration_keeps_native_routing_and_picker_metadata(
    tmp_path: Path, provider_fields: str
) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        "[model_providers.openrouter]\n"
        + provider_fields
        + '[model_providers.openrouter.provider]\nonly = ["wafer/mxfp4"]\n'
        "allow_fallbacks = false\n",
        encoding="utf-8",
    )
    model = "deepseek/deepseek-v4.1-flash-1017"
    models = tmp_path / "models.toml"
    models.write_text(f'[models."{model}"]\nprovider = "openrouter"\n', encoding="utf-8")
    registration = load_localdex_config(providers, models_path=models, require_token=False)
    assert localdex_runtime_provider_id("openrouter") == "openrouter"
    provider_config = tomllib.loads(localdex_provider_config_overrides(registration)[0])[
        "model_providers"
    ]["openrouter"]
    assert "name" not in provider_config
    assert provider_config["env_key"] == "OPENROUTER_API_KEY"
    assert provider_config["base_url"] == "https://openrouter.ai/api/v1"
    assert provider_config["provider"] == {
        "only": ["wafer/mxfp4"],
        "allow_fallbacks": False,
    }
    source = {
        "id": model,
        "model": model,
        "displayName": "DS v4.1 Flash 1017 (OpenRouter)",
        "defaultReasoningEffort": "max",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort}
            for effort in ["off", "low", "high", "max"]
        ],
        "supportsPersonality": False,
        "inputModalities": ["text"],
    }
    assert with_localdex_model_picker_row([source], registration) == [
        {
            **source,
            "displayName": "DeepSeek v4.1 Flash",
            "modelFamily": "ds-v4.1-flash-1017",
            "modelLabel": "DeepSeek v4.1 Flash",
            "providerId": "openrouter",
            "variant": "regular",
        }
    ]


def test_named_localdex_models_map_to_their_explicit_provider(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.dsv]
base_url = "http://127.0.0.1:9000/v1"
env_key = "DSV_TOKEN"
wire_api = "responses"
requires_openai_auth = false

[model_providers.lab]
base_url = "https://models.example/v1"
env_key = "LAB_TOKEN"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text(
        """
[models."DS v4.1 Flash 1017"]
provider = "dsv"
display_name = "DS v4.1 Flash 1017"
discover_capabilities = true

[models."lab/coder-32b"]
provider = "lab"
display_name = "Lab Coder"
""",
        encoding="utf-8",
    )

    registration = load_localdex_config(providers, models_path=models, require_token=False)

    dsv = localdex_model_for_selection(registration, "DS v4.1 Flash 1017")
    assert dsv is not None
    assert (dsv.provider, dsv.env_key, dsv.base_url, dsv.discover_capabilities) == (
        "dsv",
        "DSV_TOKEN",
        "http://127.0.0.1:9000/v1",
        True,
    )
    assert localdex_model_for_selection(registration, "lab/coder-32b").provider == "lab"
    assert localdex_model_for_selection(registration, "gpt-6-sol") is None

    local_overrides = localdex_provider_config_overrides(registration)
    provider_config = tomllib.loads(local_overrides[0])["model_providers"]
    dsv_runtime_id = localdex_runtime_provider_id("dsv")
    assert dsv_runtime_id != "openai"
    assert localdex_runtime_provider_id("localdex") == "localdex"
    assert provider_config[dsv_runtime_id]["base_url"] == "http://127.0.0.1:9000/v1"
    assert provider_config[dsv_runtime_id]["env_key"] == "DSV_TOKEN"
    assert provider_config[dsv_runtime_id]["requires_openai_auth"] is False
    assert "test-secret-value" not in repr(provider_config[dsv_runtime_id])

    rows = with_localdex_model_picker_row([], registration)
    assert [(row["id"], row["displayName"]) for row in rows] == [
        ("DS v4.1 Flash 1017", "DeepSeek v4.1 Flash"),
        ("lab/coder-32b", "Lab Coder"),
    ]
    assert rows[0]["isDefault"] is True
    assert rows[0]["defaultReasoningEffort"] == "max"
    assert rows[0]["supportedReasoningEfforts"][0]["reasoningEffort"] == "off"


def test_localdex_row_overrides_an_upstream_default(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.localdex]
base_url = "http://127.0.0.1:9000/v1"
env_key = "LOCALDEX_API_KEY"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text(
        '[models."DS v4.1 Flash 1017"]\nprovider = "localdex"\n',
        encoding="utf-8",
    )
    registration = load_localdex_config(providers, models_path=models, require_token=False)

    rows = with_localdex_model_picker_row(
        [{"id": "gpt-6-sol", "displayName": "GPT-6 Sol", "isDefault": True}],
        registration,
    )

    assert rows[0]["id"] == "DS v4.1 Flash 1017"
    assert rows[0]["isDefault"] is True
    assert "isDefault" not in rows[1]


def test_localdex_provider_materialization_preserves_codex_provider_routes(tmp_path: Path) -> None:
    """A private LocalDex table is additive to the user's official/gateway config."""
    from omnigent.inner.codex_executor import materialize_codex_provider_config

    providers = tmp_path / "localdex-config.toml"
    providers.write_text(
        """
[model_providers.openai]
name = "Local OpenAI-compatible endpoint"
base_url = "https://local.example/v1"
env_key = "LOCAL_TOKEN"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "openai"\n', encoding="utf-8")
    registration = load_localdex_config(providers, models_path=models, require_token=False)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
model_provider = "gateway"

[model_providers.openai]
name = "OpenAI"
base_url = "https://api.openai.com/v1"

[model_providers.gateway]
name = "Gateway"
base_url = "https://gateway.example/v1"
""",
        encoding="utf-8",
    )

    argv_overrides = materialize_codex_provider_config(
        codex_home, list(localdex_provider_config_overrides(registration))
    )

    assert argv_overrides == []
    resulting = tomllib.loads((codex_home / "config.toml").read_text())
    providers_after = resulting["model_providers"]
    runtime_id = localdex_runtime_provider_id("openai")
    assert resulting["model_provider"] == "gateway"
    assert providers_after["openai"]["base_url"] == "https://api.openai.com/v1"
    assert providers_after["gateway"]["base_url"] == "https://gateway.example/v1"
    assert providers_after[runtime_id]["base_url"] == "https://local.example/v1"
    assert providers_after[runtime_id]["env_key"] == "LOCAL_TOKEN"


def test_localdex_model_registry_rejects_non_responses_and_authful_providers(
    tmp_path: Path,
) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.bad]
base_url = "https://models.example/v1"
env_key = "MODEL_TOKEN"
wire_api = "chat"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "bad"\n', encoding="utf-8")

    try:
        load_localdex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert 'wire_api = "responses"' in str(exc)
    else:
        raise AssertionError("invalid custom provider was accepted")


def test_localdex_model_registry_rejects_inline_provider_secrets(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.bad]
base_url = "https://models.example/v1"
env_key = "MODEL_TOKEN"
api_key = "must-stay-out-of-config"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "bad"\n', encoding="utf-8")

    try:
        load_localdex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert "must use env_key, not inline credentials" in str(exc)
    else:
        raise AssertionError("inline LocalDex provider credentials were accepted")
