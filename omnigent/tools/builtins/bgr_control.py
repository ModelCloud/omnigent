"""Authenticated Brain controller actions on the ordinary Omnigent MCP relay."""

from typing import Any

from omnigent.bgr_control import BGR_CONTROL_TOOL, BgrControl
from omnigent.tools.base import Tool


class SysBgrControlTool(Tool):
    @classmethod
    def name(cls) -> str:
        return BGR_CONTROL_TOOL

    @classmethod
    def description(cls) -> str:
        return (
            "Brain-only durable team control: create a Grunt/task, start an independent review "
            "round, read barrier-safe status, adjudicate stopped work, inspect sanitized "
            "publication, or publish the exact approved body. An uncertain action is NOT safe "
            "to repeat with a new ID. Session identity is supplied by the authenticated relay."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": BgrControl.model_json_schema(),
            },
        }
