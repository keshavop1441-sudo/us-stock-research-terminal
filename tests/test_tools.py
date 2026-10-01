import pytest
from pydantic import BaseModel, ValidationError

from app.tools.registry import ToolRegistry, ToolSpec


class EchoArgs(BaseModel):
    text: str


def echo_spec(name="echo"):
    return ToolSpec(name=name, description="echo", args_model=EchoArgs, handler=lambda args: args.text)


def test_registry_starts_empty():
    assert ToolRegistry().names() == []


def test_call_validates_arguments():
    registry = ToolRegistry()
    registry.register(echo_spec())
    assert registry.call("echo", {"text": "hi"}) == "hi"
    with pytest.raises(ValidationError):
        registry.call("echo", {})


def test_unknown_tool_is_rejected():
    with pytest.raises(KeyError):
        ToolRegistry().call("run_sql", {"query": "DROP TABLE securities"})


def test_duplicate_and_invalid_names_are_rejected():
    registry = ToolRegistry()
    registry.register(echo_spec())
    with pytest.raises(ValueError):
        registry.register(echo_spec())
    with pytest.raises(ValueError):
        registry.register(echo_spec("Bad Name; rm -rf"))
