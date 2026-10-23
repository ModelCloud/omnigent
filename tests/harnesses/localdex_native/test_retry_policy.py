from __future__ import annotations

from pathlib import Path

import yaml


def test_materialized_localdex_spec_disables_stream_retries(tmp_path: Path) -> None:
    """Generated LocalDex sessions must not replay a failed large prefill."""
    from omnigent.harnesses.localdex_native.main import _materialize_localdex_agent_spec

    spec_path = _materialize_localdex_agent_spec(
        tmp_path,
        model="QB/DSV4.1-Flash",
    )
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert spec["executor"]["model"] == "QB/DSV4.1-Flash"
    assert spec["executor"]["reasoning_effort"] == "max"
    assert spec["llm"] == {
        "model": "QB/DSV4.1-Flash",
        "retry": {
            "max_retries": 0,
            "timeout_per_request_s": 900,
        },
    }
