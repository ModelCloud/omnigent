"""Regression: a local-model session must keep the official provider table.

A session launched on MultiDex's local model can be switched to an official
OpenAI/GPT model from the web picker. Omnigent applies that switch with
``thread/settings/update`` naming ``omnigent_provider``, and the Codex
app-server rejects a provider name that is not defined in the session's
private ``config.toml``:

    -32600 invalid thread settings override: invalid value for
    `model_provider`: `omnigent_provider` is not in the allowed set

``_local_model_config_overrides`` therefore replaces only the selection keys
(``model_provider`` / ``model`` / ``model_reasoning_summary``) and carries
every provider table through from the resolved non-local launch.
"""

from __future__ import annotations

import json

from omnigent.runner.native.orchestration import _local_model_config_overrides

_OFFICIAL_PROVIDER_TABLE = (
    'model_providers.omnigent_provider={name="Omnigent Provider",'
    'base_url="https://api.openai.com/v1",'
    'auth={command="sh",args=["-c","printf %s tok"]},'
    'wire_api="responses"}'
)


def test_official_provider_table_survives_local_model_selection() -> None:
    existing = [
        'model="gpt-6-astra"',
        'model_provider="omnigent_provider"',
        _OFFICIAL_PROVIDER_TABLE,
        "show_raw_agent_reasoning=true",
    ]

    result = _local_model_config_overrides(
        existing,
        provider="multidex",
        model="deepseek/deepseek-v4.1-flash-1017",
    )

    assert 'model_provider="multidex"' in result
    assert f"model={json.dumps('deepseek/deepseek-v4.1-flash-1017')}" in result
    assert _OFFICIAL_PROVIDER_TABLE in result
    # The official selection keys are replaced, not duplicated.
    assert 'model_provider="omnigent_provider"' not in result
    assert 'model="gpt-6-astra"' not in result


def test_literal_provider_section_headers_are_preserved() -> None:
    existing = [
        'model_provider="omnigent_provider"',
        "[model_providers.omnigent_provider]",
        'name="Omnigent Provider"',
    ]

    result = _local_model_config_overrides(
        existing, provider="multidex", model="deepseek/deepseek-v4.1-flash-1017"
    )

    assert "[model_providers.omnigent_provider]" in result


def test_selection_keys_are_replaced_even_when_absent() -> None:
    result = _local_model_config_overrides([], provider="multidex", model="m")

    assert result == [
        'model_provider="multidex"',
        'model="m"',
        'model_reasoning_summary="none"',
    ]
