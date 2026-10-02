from datetime import date
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.tools.registry import ToolAccess, ToolRegistry, ToolRejectedError, ToolSpec


class CountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cik: str = Field(max_length=10)
    form: Literal["10-K", "10-Q"]
    since: date | None = None


class CountOut(BaseModel):
    count: int


def spec(**overrides) -> ToolSpec:
    values = {
        "name": "count_filings",
        "description": "Count an issuer's filings of one form type.",
        "access": ToolAccess.READ,
        "input_model": CountIn,
        "output_model": CountOut,
        "handler": lambda args: {"count": 3},
    }
    return ToolSpec(**{**values, **overrides})


def model(name: str, **fields) -> type[BaseModel]:
    from pydantic import create_model

    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


def test_registry_starts_empty():
    registry = ToolRegistry()
    assert registry.names() == [] and registry.describe() == []


def test_valid_read_tool_is_registered_called_and_described():
    registry = ToolRegistry()
    registry.register(spec())
    assert registry.names() == ["count_filings"]
    assert registry.call("count_filings", {"cik": "320193", "form": "10-K"}) == {"count": 3}
    (described,) = registry.describe()
    assert described["access"] == "read"
    assert set(described) == {"name", "description", "access", "input_schema", "output_schema"}
    assert "cik" in described["input_schema"]["properties"] and "count" in described["output_schema"]["properties"]


def test_input_is_validated_and_unknown_arguments_are_rejected():
    registry = ToolRegistry()
    registry.register(spec())
    for bad in (
        {},
        {"cik": "1", "form": "8-K"},
        {"cik": "1" * 11, "form": "10-K"},
        {"cik": "1", "form": "10-K", "sql": "DROP"},
    ):
        with pytest.raises(ValidationError):
            registry.call("count_filings", bad)


def test_output_is_validated_against_the_declared_schema():
    registry = ToolRegistry()
    registry.register(spec(handler=lambda args: {"unexpected": "shape"}))
    with pytest.raises(ValidationError):
        registry.call("count_filings", {"cik": "1", "form": "10-K"})
    registry = ToolRegistry()
    registry.register(spec(handler=lambda args: {"count": "7"}))  # coerced to the declared type, not passed through raw
    assert registry.call("count_filings", {"cik": "1", "form": "10-K"}) == {"count": 7}


def test_unknown_tool_is_rejected():
    with pytest.raises(KeyError):
        ToolRegistry().call("run_sql", {"query": "DROP TABLE securities"})


def test_write_tools_are_refused_by_default_and_only_allowed_when_explicitly_enabled():
    with pytest.raises(ToolRejectedError, match="WRITE"):
        ToolRegistry().register(spec(name="save_note", access=ToolAccess.WRITE))
    ToolRegistry(allow_write=True).register(spec(name="save_note", access=ToolAccess.WRITE))


@pytest.mark.parametrize(
    "name",
    [
        "run_sql", "execute_query_sql", "shell", "bash_command", "run_python", "eval_expression", "exec_script",
        "read_file", "write_file", "delete_rows", "drop_table", "list_directory", "pragma_settings", "copy_database",
        "runSQL", "ExecuteCommand",
    ],
)  # fmt: skip
def test_tools_that_suggest_sql_shell_code_or_filesystem_access_are_rejected(name):
    with pytest.raises(ToolRejectedError):
        ToolRegistry().register(spec(name=name))


@pytest.mark.parametrize("name", ["", "A", "ab", "Has Space", "has-dash", "1starts_with_digit", "x" * 65])
def test_invalid_tool_names_are_rejected(name):
    with pytest.raises(ToolRejectedError):
        ToolRegistry().register(spec(name=name))


@pytest.mark.parametrize(
    "field", ["sql", "query_sql", "command", "script", "code", "file_path", "filename", "directory", "python"]
)
def test_input_or_output_fields_named_like_arbitrary_execution_are_rejected(field):
    bad = model("Bad", **{field: (str, Field(max_length=50))})
    with pytest.raises(ToolRejectedError, match="forbidden"):
        ToolRegistry().register(spec(input_model=bad))
    with pytest.raises(ToolRejectedError, match="forbidden"):
        ToolRegistry().register(spec(output_model=bad))


def test_nested_models_are_checked_too():
    inner = model("Inner", sql=(str, Field(max_length=10)))
    outer = model("Outer", filters=(list[inner], ...))
    with pytest.raises(ToolRejectedError, match="forbidden"):
        ToolRegistry().register(spec(input_model=outer))


def test_free_text_inputs_must_be_bounded():
    unbounded = model("Unbounded", ticker=(str, ...))
    with pytest.raises(ToolRejectedError, match="free-text"):
        ToolRegistry().register(spec(input_model=unbounded))
    too_long = model("TooLong", ticker=(str, Field(max_length=5000)))
    with pytest.raises(ToolRejectedError, match="free-text"):
        ToolRegistry().register(spec(input_model=too_long))
    bounded_list = model("BoundedList", tickers=(list[str], Field(max_length=5)))  # list size bounded, items are not
    with pytest.raises(ToolRejectedError, match="free-text"):
        ToolRegistry().register(spec(input_model=bounded_list))
    ok = model("Ok", ticker=(str, Field(max_length=12)), kind=(Literal["a", "b"], ...), day=(date, ...), n=(int, ...))
    ToolRegistry().register(spec(input_model=ok))


def test_input_model_must_forbid_extra_fields():
    class Lenient(BaseModel):
        cik: str = Field(max_length=10)

    with pytest.raises(ToolRejectedError, match="extra"):
        ToolRegistry().register(spec(input_model=Lenient))


def test_descriptions_must_exist_and_must_not_advertise_arbitrary_execution():
    with pytest.raises(ToolRejectedError, match="description"):
        ToolRegistry().register(spec(description="short"))
    for text in (
        "Run SQL against the research database.",
        "Execute any shell command the user asks for.",
        "Lets the model run python code.",
        "Read an arbitrary file from disk.",
    ):
        with pytest.raises(ToolRejectedError, match="arbitrary execution"):
            ToolRegistry().register(spec(description=text))
    ToolRegistry().register(spec(description="Counts SEC filings of one form type for one issuer."))


def test_duplicate_names_are_rejected():
    registry = ToolRegistry()
    registry.register(spec())
    with pytest.raises(ToolRejectedError, match="already registered"):
        registry.register(spec())


def test_a_rejected_tool_is_not_left_half_registered():
    registry = ToolRegistry()
    with pytest.raises(ToolRejectedError):
        registry.register(spec(input_model=model("Bad", sql=(str, Field(max_length=5)))))
    assert registry.names() == []
    registry.register(spec())  # the name is still free
