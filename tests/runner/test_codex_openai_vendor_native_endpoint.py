"""OpenAI-vendor models route to OpenAI's native endpoint, never a gateway.

MultiDex is a Codex fork, so it speaks OpenAI's Responses API natively and the
built-in ``openai`` provider reaches ``api.openai.com`` directly. When the
Codex default is a ``subscription`` that is not logged in, the resolver falls
through to the first provider that serves the ``openai`` *family* — a family
that glue gateways (OpenRouter, Lithos) also declare, because they speak the
same wire. Falling through to one of those for an OpenAI-vendor id is wrong
twice over:

* the gateway need not carry that vendor at all, which codex surfaces as
  ``No allowed providers are available for the selected model`` (a 404 on a
  provider allowlist, not a model error); and
* even when it does, it puts a proxy hop and a resale margin in front of a call
  Codex can make directly.

An OpenAI-vendor pin therefore only falls through to a provider that is
OpenAI's own endpoint; everything else is skipped so the launch lands on
Codex's native OpenAI login instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_GATEWAYS_ONLY = """
providers:
  codex:
    cli: codex
    default: openai
    kind: subscription
  openrouter:
    kind: gateway
    openai:
      api_key_ref: env:OPENROUTER_API_KEY
      base_url: https://openrouter.ai/api/v1
      wire_api: responses
  lithos:
    kind: gateway
    openai:
      api_key_ref: env:LITHOS_API_KEY
      base_url: https://api.lithosai.cloud/v1
      wire_api: responses
"""

_WITH_NATIVE_OPENAI = (
    _GATEWAYS_ONLY
    + """
  openai-key:
    kind: key
    openai:
      api_key_ref: env:OPENAI_API_KEY
      base_url: https://api.openai.com/v1
      wire_api: responses
"""
)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the onboarding layer at an isolated config file."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LITHOS_API_KEY", "lithos-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    return tmp_path


def _write(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _logged_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the dead-subscription fall-through path."""
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setattr(app_server, "_codex_login_usable", lambda: False)


@pytest.mark.parametrize(
    "model",
    ["gpt-6-astra", "gpt-6-sol", "openai/gpt-6-astra", "chatgpt-4o-latest", "o3"],
)
def test_openai_vendor_model_never_falls_through_to_a_gateway(
    model: str, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendor id with only gateways configured stays on Codex's own endpoint."""
    _write(config_home, _GATEWAYS_ONLY)
    _logged_out(monkeypatch)
    from omnigent.harnesses.codex_native import app_server

    launch = app_server.resolve_native_codex_launch(model=model)

    assert "openrouter" not in launch.summary.lower()
    assert "lithos" not in launch.summary.lower()
    rendered = " ".join(launch.config_overrides)
    assert "openrouter.ai" not in rendered
    assert "lithosai.cloud" not in rendered


def test_openai_vendor_model_falls_through_to_openai_when_configured(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed OpenAI endpoint is a valid fall-through; a gateway is not."""
    _write(config_home, _WITH_NATIVE_OPENAI)
    _logged_out(monkeypatch)
    from omnigent.harnesses.codex_native import app_server

    launch = app_server.resolve_native_codex_launch(model="gpt-6-astra")

    assert launch.summary.startswith("provider 'openai-key'")
    rendered = " ".join(launch.config_overrides)
    assert "api.openai.com" in rendered
    assert "openrouter.ai" not in rendered


@pytest.mark.parametrize("model", ["glm-5.3", "kimi-k3", "deepseek/deepseek-v4.1-flash-1017"])
def test_non_openai_vendor_models_still_route_to_a_gateway(
    model: str, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GLM / Kimi / DeepSeek are gateway-hosted; the guard must not block them."""
    _write(config_home, _GATEWAYS_ONLY)
    _logged_out(monkeypatch)
    from omnigent.harnesses.codex_native import app_server

    launch = app_server.resolve_native_codex_launch(model=model)

    assert launch.summary.startswith("provider 'openrouter'")


def test_gateway_with_openai_base_url_is_still_not_native(
    config_home: Path,
) -> None:
    """A ``gateway`` kind pointed at api.openai.com is still a proxy, not native."""
    from omnigent.harnesses.codex_native.app_server import _serves_openai_vendor_natively
    from omnigent.onboarding import provider_config as pc

    _write(
        config_home,
        """
providers:
  proxied-openai:
    kind: gateway
    openai:
      api_key_ref: env:OPENAI_API_KEY
      base_url: https://api.openai.com/v1
      wire_api: responses
""",
    )
    entry = pc.load_providers(pc._load_config()).get("proxied-openai")
    assert entry is not None
    assert _serves_openai_vendor_natively(entry) is False


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-astra", True),
        ("openai/gpt-6-astra", True),
        ("chatgpt-4o-latest", True),
        ("o3", True),
        ("glm-5.3", False),
        ("kimi-k3", False),
        ("deepseek/deepseek-v4.1-flash-1017", False),
        ("", False),
        (None, False),
    ],
)
def test_vendor_detection(model: str | None, expected: bool) -> None:
    from omnigent.harnesses.codex_native.app_server import _is_openai_vendor_model

    assert _is_openai_vendor_model(model) is expected
