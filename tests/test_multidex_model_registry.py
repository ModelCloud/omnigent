from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from omnigent.harnesses.multidex_native.config import (
    MULTIDEX_BUILTIN_PROVIDER_OVERRIDE_FIELDS,
    load_multidex_config,
    multidex_model_for_selection,
    multidex_provider_config_overrides,
    multidex_registration_bearer_token,
    multidex_runtime_provider_id,
    with_multidex_model_picker_row,
)


@pytest.mark.parametrize("explicit_fast", [False, True])
def test_lithos_provider_registers_three_variants_with_one_model_label(
    tmp_path: Path, explicit_fast: bool
) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        '[model_providers.lithos]\nexperimental_bearer_token = "lith-test-token"\n'
        "request_max_retries = 0\nstream_max_retries = 0\nstream_idle_timeout_ms = 900000\n",
        encoding="utf-8",
    )
    models_path = tmp_path / "models.toml"
    if explicit_fast:
        models_path.write_text(
            '[models."deepseek-ai/DeepSeek-V4.1-Flash-fast"]\nprovider = "lithos"\n',
            encoding="utf-8",
        )
    registration = load_multidex_config(providers, models_path=models_path, require_token=False)
    rows = with_multidex_model_picker_row([], registration)
    assert {row["modelFamily"] for row in rows} == {"ds-v4.1-flash-1017"}
    assert {row["displayName"] for row in rows} == {"DeepSeek v4.1 Flash"}
    assert {row["providerId"] for row in rows} == {"lithos"}
    assert {row["variant"] for row in rows} == {"regular", "fast", "ultra"}
    assert all(row["defaultReasoningEffort"] == "max" for row in rows)
    assert all(
        [level["reasoningEffort"] for level in row["supportedReasoningEfforts"]]
        == ["none", "low", "high", "max"]
        for row in rows
    )
    assert next(row for row in rows if row["variant"] == "regular")["id"] == (
        "deepseek-ai/DeepSeek-V4.1-Flash"
    )
    native = tomllib.loads(multidex_provider_config_overrides(registration)[0])["model_providers"]
    assert "lithos" in native
    assert "name" not in native["lithos"]
    assert native["lithos"]["base_url"] == "https://api.lithosai.cloud/v1"
    assert "env_key" not in native["lithos"]
    assert set(native["lithos"]) <= MULTIDEX_BUILTIN_PROVIDER_OVERRIDE_FIELDS


@pytest.mark.parametrize(
    "provider_fields",
    [
        'experimental_bearer_token = "or-test-token"\n',
        'base_url = "https://openrouter.ai/api/v1"\nexperimental_bearer_token = "or-test-token"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nname = "OpenRouter"\n',
        'experimental_bearer_token = "or-test-token"\n'
        "request_max_retries = 0\nstream_max_retries = 0\nstream_idle_timeout_ms = 900000\n",
    ],
)
@pytest.mark.parametrize("default_effort", ["max", "off"])
def test_openrouter_registration_keeps_native_routing_and_picker_metadata(
    tmp_path: Path, provider_fields: str, default_effort: str
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
    registration = load_multidex_config(providers, models_path=models, require_token=False)
    assert multidex_runtime_provider_id("openrouter") == "openrouter"
    provider_config = tomllib.loads(multidex_provider_config_overrides(registration)[0])[
        "model_providers"
    ]["openrouter"]
    assert set(provider_config) <= MULTIDEX_BUILTIN_PROVIDER_OVERRIDE_FIELDS
    assert "env_key" not in provider_config
    assert provider_config["experimental_bearer_token"] == "or-test-token"
    assert provider_config["base_url"] == "https://openrouter.ai/api/v1"
    assert provider_config["provider"] == {
        "only": ["wafer/mxfp4"],
        "allow_fallbacks": False,
    }
    source = {
        "id": model,
        "model": model,
        "displayName": "DS v4.1 Flash 1017 (OpenRouter)",
        "defaultReasoningEffort": default_effort,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort}
            for effort in ["off", "low", "high", "max"]
        ],
        "supportsPersonality": False,
        "inputModalities": ["text"],
    }
    expected = [
        {
            **source,
            "displayName": "DeepSeek v4.1 Flash",
            "modelFamily": "ds-v4.1-flash-1017",
            "modelLabel": "DeepSeek v4.1 Flash",
            "providerId": "openrouter",
            "variant": "regular",
            "defaultReasoningEffort": "none" if default_effort == "off" else default_effort,
            "supportedReasoningEfforts": [
                {"reasoningEffort": "none", "description": "off"},
                *source["supportedReasoningEfforts"][1:],
            ],
        }
    ]
    assert with_multidex_model_picker_row([source], registration) == expected
    assert with_multidex_model_picker_row([source], None) == expected
    assert source["defaultReasoningEffort"] == default_effort
    assert source["supportedReasoningEfforts"][0]["reasoningEffort"] == "off"


def test_named_multidex_models_map_to_their_explicit_provider(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.dsv]
base_url = "http://127.0.0.1:9000/v1"
experimental_bearer_token = "dsv-test-token"
wire_api = "responses"
requires_openai_auth = false

[model_providers.lab]
base_url = "https://models.example/v1"
experimental_bearer_token = "lab-test-token"
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

    registration = load_multidex_config(providers, models_path=models, require_token=False)

    dsv = multidex_model_for_selection(registration, "DS v4.1 Flash 1017")
    assert dsv is not None
    assert (dsv.provider, dsv.base_url, dsv.discover_capabilities) == (
        "dsv",
        "http://127.0.0.1:9000/v1",
        True,
    )
    assert multidex_registration_bearer_token(dsv) == "dsv-test-token"
    assert multidex_model_for_selection(registration, "lab/coder-32b").provider == "lab"
    assert multidex_model_for_selection(registration, "gpt-6-sol") is None

    local_overrides = multidex_provider_config_overrides(registration)
    provider_config = tomllib.loads(local_overrides[0])["model_providers"]
    dsv_runtime_id = multidex_runtime_provider_id("dsv")
    assert dsv_runtime_id != "openai"
    assert multidex_runtime_provider_id("multidex") == "multidex"
    assert provider_config[dsv_runtime_id]["base_url"] == "http://127.0.0.1:9000/v1"
    assert provider_config[dsv_runtime_id]["experimental_bearer_token"] == "dsv-test-token"
    assert "env_key" not in provider_config[dsv_runtime_id]
    assert provider_config[dsv_runtime_id]["requires_openai_auth"] is False
    assert "test-secret-value" not in repr(provider_config[dsv_runtime_id])

    rows = with_multidex_model_picker_row([], registration)
    assert [(row["id"], row["displayName"]) for row in rows] == [
        ("DS v4.1 Flash 1017", "DeepSeek v4.1 Flash"),
        ("lab/coder-32b", "Lab Coder"),
    ]
    assert rows[0]["isDefault"] is True
    assert rows[0]["defaultReasoningEffort"] == "max"
    assert rows[0]["supportedReasoningEfforts"][0]["reasoningEffort"] == "none"


def test_multidex_row_overrides_an_upstream_default(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.multidex]
base_url = "http://127.0.0.1:9000/v1"
experimental_bearer_token = "mdx-test-token"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text(
        '[models."DS v4.1 Flash 1017"]\nprovider = "multidex"\n',
        encoding="utf-8",
    )
    registration = load_multidex_config(providers, models_path=models, require_token=False)

    rows = with_multidex_model_picker_row(
        [{"id": "gpt-6-sol", "displayName": "GPT-6 Sol", "isDefault": True}],
        registration,
    )

    assert rows[0]["id"] == "DS v4.1 Flash 1017"
    assert rows[0]["isDefault"] is True
    assert "isDefault" not in rows[1]


def test_multidex_provider_materialization_preserves_codex_provider_routes(tmp_path: Path) -> None:
    """A private MultiDex table is additive to the user's official/gateway config."""
    from omnigent.inner.codex_executor import materialize_codex_provider_config

    providers = tmp_path / "multidex-config.toml"
    providers.write_text(
        """
[model_providers.openai]
name = "Local OpenAI-compatible endpoint"
base_url = "https://local.example/v1"
experimental_bearer_token = "local-test-token"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "openai"\n', encoding="utf-8")
    registration = load_multidex_config(providers, models_path=models, require_token=False)
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
        codex_home, list(multidex_provider_config_overrides(registration))
    )

    assert argv_overrides == []
    resulting = tomllib.loads((codex_home / "config.toml").read_text())
    providers_after = resulting["model_providers"]
    runtime_id = multidex_runtime_provider_id("openai")
    assert resulting["model_provider"] == "gateway"
    assert providers_after["openai"]["base_url"] == "https://api.openai.com/v1"
    assert providers_after["gateway"]["base_url"] == "https://gateway.example/v1"
    assert providers_after[runtime_id]["base_url"] == "https://local.example/v1"
    assert providers_after[runtime_id]["experimental_bearer_token"] == "local-test-token"
    assert "env_key" not in providers_after[runtime_id]


def test_multidex_model_registry_rejects_non_responses_and_authful_providers(
    tmp_path: Path,
) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.bad]
base_url = "https://models.example/v1"
experimental_bearer_token = "model-test-token"
wire_api = "chat"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "bad"\n', encoding="utf-8")

    try:
        load_multidex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert 'wire_api = "responses"' in str(exc)
    else:
        raise AssertionError("invalid custom provider was accepted")


def test_provider_without_any_credential_is_rejected(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.lab]
base_url = "https://models.example/v1"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "lab"\n', encoding="utf-8")

    try:
        load_multidex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert "experimental_bearer_token" in str(exc)
    else:
        raise AssertionError("credential-less provider was accepted")


def test_multidex_model_registry_rejects_inline_provider_secrets(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.bad]
base_url = "https://models.example/v1"
experimental_bearer_token = "model-test-token"
api_key = "must-stay-out-of-config"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "bad"\n', encoding="utf-8")

    try:
        load_multidex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert "experimental_bearer_token, not inline credentials" in str(exc)
    else:
        raise AssertionError("inline MultiDex provider credentials were accepted")


def test_multidex_provider_bearer_token_replaces_env_key(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.lab]
base_url = "https://models.example/v1"
experimental_bearer_token = "config-token"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "lab"\n', encoding="utf-8")

    registration = load_multidex_config(providers, models_path=models, require_token=True)

    selected = multidex_model_for_selection(registration, "vendor/model")
    assert selected is not None
    assert multidex_registration_bearer_token(selected) == "config-token"

    provider_config = tomllib.loads(multidex_provider_config_overrides(registration)[0])[
        "model_providers"
    ][multidex_runtime_provider_id("lab")]
    assert provider_config["experimental_bearer_token"] == "config-token"
    assert "env_key" not in provider_config


def test_builtin_provider_bearer_token_drops_default_env_key(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.lithos]
experimental_bearer_token = "lith-config-token"
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    registration = load_multidex_config(providers, models_path=models, require_token=True)

    lithos = next(item for item in registration.models if item.provider == "lithos")
    assert multidex_registration_bearer_token(lithos) == "lith-config-token"

    native = tomllib.loads(multidex_provider_config_overrides(registration)[0])["model_providers"][
        "lithos"
    ]
    assert native["experimental_bearer_token"] == "lith-config-token"
    assert "env_key" not in native
    assert set(native) <= MULTIDEX_BUILTIN_PROVIDER_OVERRIDE_FIELDS


def test_provider_env_key_is_rejected(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text(
        """
[model_providers.lab]
base_url = "https://models.example/v1"
env_key = "LAB_TOKEN"
wire_api = "responses"
requires_openai_auth = false
""",
        encoding="utf-8",
    )
    models = tmp_path / "models.toml"
    models.write_text('[models."vendor/model"]\nprovider = "lab"\n', encoding="utf-8")

    try:
        load_multidex_config(providers, models_path=models, require_token=False)
    except ValueError as exc:
        assert "does not support env_key" in str(exc)
        assert "experimental_bearer_token" in str(exc)
    else:
        raise AssertionError("env_key provider credential was accepted")


def test_builtin_provider_without_token_is_rejected(tmp_path: Path) -> None:
    providers = tmp_path / "config.toml"
    providers.write_text("[model_providers.lithos]\n", encoding="utf-8")
    models = tmp_path / "models.toml"

    try:
        load_multidex_config(providers, models_path=models, require_token=True)
    except ValueError as exc:
        assert "experimental_bearer_token" in str(exc)
    else:
        raise AssertionError("credential-less lithos provider was accepted")
