"""Brain tool transport; no SQL, scheduler or forge credentials in the relay."""

import json
from typing import Any

import httpx
from pydantic import ValidationError

from omnigent.bgr_control import BgrControl
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER


async def execute_bgr_control(
    arguments: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    if client is None or source_id is None:
        return json.dumps({"error": "BGR requires an authenticated Brain relay"})
    try:
        command = BgrControl.model_validate(arguments)
    except ValidationError:
        return json.dumps({"error": "Invalid BGR action fields"})
    try:
        response = await client.post(
            "/v1/bgr/control",
            json=command.model_dump(mode="json", exclude_none=True),
            headers={ORCHESTRATION_SOURCE_HEADER: source_id},
            timeout=120,
        )
        if response.is_error:
            return json.dumps(
                {"error": "BGR action refused or unconfirmed", "status": response.status_code}
            )
        return json.dumps(response.json())
    except (httpx.HTTPError, ValueError):
        return json.dumps(
            {
                "error": "BGR outcome uncertain; inspect status, never blindly repeat effects",
                "action_id": command.action_id,
            }
        )
