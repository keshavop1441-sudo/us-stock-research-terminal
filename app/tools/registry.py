"""Registry of the only operations an LLM agent may request.

An agent can call a tool only by name, with arguments validated against the tool's declared input
model, and gets back output validated against the declared output model. Every tool must declare its
name, description, input schema, output schema and whether it is READ or WRITE.

What the registry enforces at registration time
-----------------------------------------------
* WRITE tools are refused unless the registry was created with ``allow_write=True`` (the application
  never does this today).
* Names (tool and every field in its schemas) may not suggest arbitrary SQL, shell, code execution,
  filesystem paths or destructive operations.
* The input model must forbid unknown fields, and every free-text field must be bounded (a maximum
  length, an enum/literal, or a date), so a model cannot smuggle a script or SQL statement through a
  generic text parameter.

Honest limit: the registry validates declarations, not handler bodies. The real boundary is that
handlers are written to call only explicit methods of ``ReadRepository`` (parameterised SQL) and never
receive a ``WriteRepository`` or a raw connection. Code review of every new tool stays mandatory.
"""

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
MAX_TEXT_LENGTH = 200

# Tokens (split on '_' / camelCase) that must not appear in tool or field names.
FORBIDDEN_NAME_TOKENS = frozenset(
    {
        "sql", "query_sql", "shell", "bash", "powershell", "cmd", "command", "exec", "execute", "eval",
        "script", "code", "python", "subprocess", "system", "os", "path", "filepath", "filename", "file",
        "directory", "folder", "delete", "remove", "drop", "truncate", "pragma", "attach", "copy", "write",
        "unlink", "chmod", "raw",
    }
)  # fmt: skip
# Description phrases that betray a tool exposing arbitrary execution.
FORBIDDEN_DESCRIPTION = re.compile(
    r"\b(run|execute|exec)\b.{0,20}\b(sql|shell|command|code|script|python)\b"
    r"|\barbitrary (sql|code|command|file|path)\b",
    re.IGNORECASE,
)


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolRejectedError(ValueError):
    """A tool declaration violates the registry's safety rules."""


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    access: ToolAccess
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[Any], Any]  # receives the validated input_model instance


def _tokens(name: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return {t for t in re.split(r"[_\W]+", spaced.lower()) if t}


def _check_name(label: str, name: str) -> None:
    bad = _tokens(name) & FORBIDDEN_NAME_TOKENS
    if bad:
        raise ToolRejectedError(f"{label} {name!r} uses forbidden term(s) {sorted(bad)}.")


def _walk_schema(
    node: Any, defs: dict[str, Any], visit: Callable[[str, dict], None], seen: frozenset[str] = frozenset()
) -> None:
    """Visit every property and string node of a JSON schema, following $ref/anyOf/items."""
    if not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name not in seen:
            _walk_schema(defs.get(name, {}), defs, visit, seen | {name})
        return
    for key in ("anyOf", "oneOf", "allOf"):
        for option in node.get(key, []):
            _walk_schema(option, defs, visit, seen)
    if node.get("type") == "string":
        visit("string", node)
    if "items" in node:
        _walk_schema(node["items"], defs, visit, seen)
    if isinstance(node.get("additionalProperties"), dict):
        _walk_schema(node["additionalProperties"], defs, visit, seen)
    for field, sub in node.get("properties", {}).items():
        visit("field", {"name": field})
        _walk_schema(sub, defs, visit, seen)


def _check_models(spec: ToolSpec) -> None:
    if spec.input_model.model_config.get("extra") != "forbid":
        raise ToolRejectedError("Input model must set extra='forbid' so unknown arguments are rejected.")

    for label, model in (("input", spec.input_model), ("output", spec.output_model)):
        schema = model.model_json_schema()
        defs = schema.get("$defs", {})

        def visit(kind: str, node: dict, label: str = label) -> None:
            if kind == "field":
                _check_name(f"{label} field", node["name"])
            elif label == "input":
                bounded = (
                    node.get("maxLength", MAX_TEXT_LENGTH + 1) <= MAX_TEXT_LENGTH
                    or "enum" in node
                    or "const" in node
                    or node.get("format") in {"date", "date-time"}
                )
                if not bounded:
                    raise ToolRejectedError(
                        f"Every free-text input field needs max_length <= {MAX_TEXT_LENGTH}, an enum/Literal or a date."
                    )

        _walk_schema(schema, defs, visit)


class ToolRegistry:
    def __init__(self, *, allow_write: bool = False) -> None:
        self._allow_write = allow_write
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not _NAME.match(spec.name):
            raise ToolRejectedError(f"Invalid tool name: {spec.name!r}")
        _check_name("Tool", spec.name)
        if len(spec.description.strip()) < 10:
            raise ToolRejectedError("Tool description must explain what the tool does (>= 10 characters).")
        if FORBIDDEN_DESCRIPTION.search(spec.description):
            raise ToolRejectedError("Tool description suggests arbitrary execution, which is not allowed.")
        if spec.access is ToolAccess.WRITE and not self._allow_write:
            raise ToolRejectedError("WRITE tools are not allowed in this registry.")
        if spec.name in self._tools:
            raise ToolRejectedError(f"Tool already registered: {spec.name}")
        _check_models(spec)
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        """What an agent is told about each tool: name, description, access and both JSON schemas."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "access": s.access.value,
                "input_schema": s.input_model.model_json_schema(),
                "output_schema": s.output_model.model_json_schema(),
            }
            for s in (self._tools[n] for n in self.names())
        ]

    def call(self, name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
        """Validate ``raw_args``, run the tool, validate and return its output. Unknown tools raise KeyError."""
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"Unknown tool: {name}")
        result = spec.handler(spec.input_model.model_validate(raw_args))
        return spec.output_model.model_validate(result).model_dump(mode="json")
