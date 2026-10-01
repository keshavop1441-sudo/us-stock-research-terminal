"""Registry of the only operations an LLM agent may request.

An agent can call a tool only by name, with arguments validated against the tool's pydantic
model. There is deliberately no generic "run SQL", "run shell" or "run Python" tool and no
tool with direct database write access. Tools added later must wrap a specific, reviewed,
parameterised operation (typically read-only helpers from ``app.database.repository``).
"""

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Any]  # receives the validated args_model instance


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not _NAME.match(spec.name):
            raise ValueError(f"Invalid tool name: {spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, raw_args: dict[str, Any]) -> Any:
        """Validate ``raw_args`` and run the tool. Unknown tools and bad arguments raise."""
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"Unknown tool: {name}")
        return spec.handler(spec.args_model.model_validate(raw_args))
