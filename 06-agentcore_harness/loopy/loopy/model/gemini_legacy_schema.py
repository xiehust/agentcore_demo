from typing import Any

from google.genai import types as genai_types
from strands.models.gemini import GeminiModel
from strands.types.tools import ToolSpec

_TYPE_MAP = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _to_google_schema(json_schema: dict[str, Any]) -> genai_types.Schema:
    """Convert a JSON Schema dict to a google-genai Schema object."""
    kwargs: dict[str, Any] = {}

    if "type" in json_schema:
        kwargs["type"] = _TYPE_MAP.get(json_schema["type"], "STRING")

    if "description" in json_schema:
        kwargs["description"] = json_schema["description"]

    if "enum" in json_schema:
        kwargs["enum"] = json_schema["enum"]

    if "properties" in json_schema:
        kwargs["properties"] = {
            k: _to_google_schema(v) for k, v in json_schema["properties"].items()
        }

    if "required" in json_schema:
        kwargs["required"] = json_schema["required"]

    if "items" in json_schema:
        kwargs["items"] = _to_google_schema(json_schema["items"])

    if "nullable" in json_schema:
        kwargs["nullable"] = json_schema["nullable"]

    if "format" in json_schema:
        kwargs["format"] = json_schema["format"]

    if "any_of" in json_schema:
        kwargs["any_of"] = [_to_google_schema(s) for s in json_schema["any_of"]]
    elif "anyOf" in json_schema:
        kwargs["any_of"] = [_to_google_schema(s) for s in json_schema["anyOf"]]

    return genai_types.Schema(**kwargs)


class LegacySchemaGeminiModel(GeminiModel):
    """GeminiModel subclass compatible with google-genai < 1.32.0.

    Strands >= 1.35.0 uses `parameters_json_schema` in FunctionDeclaration, which
    requires google-genai >= 1.32.0. However, the latest version released in Brazil
    is google-genai-1.18.0. This module provides a drop-in replacement that converts
    JSON Schema to Google's Schema type using the `parameters` field instead.
    """

    def _format_request_tools(self, tool_specs: list[ToolSpec] | None) -> list[genai_types.Tool]:
        tools = [
            genai_types.Tool(
                function_declarations=[
                    genai_types.FunctionDeclaration(
                        name=tool_spec["name"],
                        description=tool_spec["description"],
                        parameters=_to_google_schema(tool_spec["inputSchema"]["json"]),
                    )
                    for tool_spec in tool_specs or []
                ],
            ),
        ]
        if self.config.get("gemini_tools"):
            tools.extend(self.config["gemini_tools"])
        return tools
