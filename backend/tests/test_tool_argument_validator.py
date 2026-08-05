"""The guard that would have caught four silently-discarded task descriptions.

See app/services/tool_argument_validator for the incident these tests pin down.
"""

from types import SimpleNamespace

import pytest

from app.services.tool_argument_validator import (
    describe_problems,
    validate_tool_arguments,
)


def _spec(properties: dict | None, required=(), name="demo_tool"):
    parameters: dict = {"type": "object"}
    if properties is not None:
        parameters["properties"] = properties
    if required:
        parameters["required"] = list(required)
    return SimpleNamespace(name=name, parameters=parameters)


CREATE_TASK_LIKE = _spec(
    {
        "title": {"type": "string"},
        "project": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    required=("title",),
    name="create_task",
)


def test_the_real_incident_is_rejected():
    """create_task(description=...) must fail, not report success.

    Four tasks were created this way on 2026-08-05. Each returned
    {"action": "created"} and dropped the entire description.
    """
    problems = validate_tool_arguments(
        CREATE_TASK_LIKE, {"title": "T", "project": "p", "description": "a long spec"}
    )

    assert [p.kind for p in problems] == ["unknown"]
    assert problems[0].name == "description"
    # The caller has to learn the value is gone, not merely that a name was odd.
    assert "NOT saved" in problems[0].detail


def test_a_valid_call_produces_no_problems():
    assert (
        validate_tool_arguments(
            CREATE_TASK_LIKE, {"title": "T", "project": "p", "depends_on": ["X-1"]}
        )
        == []
    )


def test_missing_required_argument_is_reported():
    problems = validate_tool_arguments(CREATE_TASK_LIKE, {"project": "p"})
    assert [(p.kind, p.name) for p in problems] == [("missing_required", "title")]


def test_every_problem_is_reported_in_one_pass():
    """One round trip should tell the caller everything that is wrong.

    Reporting a single problem at a time turns one bad call into a sequence of
    them, and each retry of a planner-scale tool is not free.
    """
    problems = validate_tool_arguments(
        CREATE_TASK_LIKE, {"description": "x", "notes": "y", "depends_on": "X-1"}
    )
    kinds = sorted((p.kind, p.name) for p in problems)
    assert kinds == [
        ("missing_required", "title"),
        ("unknown", "description"),
        ("unknown", "notes"),
        ("wrong_type", "depends_on"),
    ]


@pytest.mark.parametrize(
    "declared,value,ok",
    [
        ("string", "x", True),
        ("string", 5, False),
        ("integer", 5, True),
        # bool is a subclass of int in Python; an integer field must not take it.
        ("integer", True, False),
        ("number", 1.5, True),
        ("boolean", True, True),
        ("array", ["a"], True),
        ("array", "a", False),
        ("object", {"a": 1}, True),
    ],
)
def test_type_checking_covers_the_obvious_mistakes(declared, value, ok):
    spec = _spec({"field": {"type": declared}})
    problems = validate_tool_arguments(spec, {"field": value})
    assert (problems == []) is ok


def test_none_is_left_alone():
    """An explicit null is the caller clearing a field, not a type error."""
    spec = _spec({"field": {"type": "string"}})
    assert validate_tool_arguments(spec, {"field": None}) == []


def test_a_field_without_a_declared_type_is_not_constrained():
    """Do not invent a constraint the schema never declared."""
    spec = _spec({"field": {"description": "anything"}})
    assert validate_tool_arguments(spec, {"field": {"nested": 1}}) == []


def test_tools_that_declare_no_properties_still_accept_anything():
    """Some tools take free-form input on purpose.

    Applying the unknown-argument check to them would break working calls while
    fixing an unrelated bug.
    """
    assert validate_tool_arguments(_spec(None), {"whatever": 1}) == []


def test_error_body_names_the_accepted_parameters_and_the_recovery():
    problems = validate_tool_arguments(
        CREATE_TASK_LIKE, {"title": "T", "description": "x"}
    )
    body = describe_problems("create_task", problems, ["title", "project", "depends_on"])

    assert body["code"] == "unknown_arguments"
    assert "description" in body["message"]
    assert "depends_on, project, title" in body["hint"]
    assert "update_task" in body["hint"]
    assert body["problems"][0]["kind"] == "unknown"


def test_type_error_alone_is_not_reported_as_unknown_arguments():
    problems = validate_tool_arguments(CREATE_TASK_LIKE, {"title": 5})
    body = describe_problems("create_task", problems, ["title"])
    assert body["code"] == "invalid_arguments"
