"""Validate tool arguments against the ToolSpec schema before anything runs.

Why this exists
---------------

On 2026-08-05 four tasks were created with a ``description=...`` argument.
``create_task`` declares only ``title``, ``project`` and ``depends_on`` — there
is no ``description`` parameter. The call still returned ``{"action": "created"}``
with no error and no warning, and every byte of those descriptions was dropped:
evidence, constraints, the "do not do this" lists, the verification steps.

Nothing surfaced the loss. It was found days later while investigating an
unrelated outage, by noticing that ``raw_input`` was empty on exactly the tasks
created that way. One of them (CTV2-1380) reached dispatch with no content at
all, was correctly refused by the fail-closed check, and landed in ``failed``
with no way back to ``todo``.

The audit that followed found this was not a ``create_task`` quirk: **none** of
the 34 registered tools set ``additionalProperties``, so JSON Schema's permissive
default applied to all of them, and FastMCP does not reject unknown keys either.
Every tool on the surface silently accepted arguments it would then throw away.

A tool that answers "created" to an argument it discarded is worse than one that
errors: the caller stores a false belief and acts on it. Failing loudly here is
cheap; the silence cost four tasks' worth of specification.

What it checks
--------------

Unknown arguments, missing required arguments, and obvious type mismatches —
each reported together rather than one per round trip, so a caller fixes the
whole call at once instead of discovering problems one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# JSON Schema type name -> Python types accepted for it. Deliberately narrow:
# this is a guard against obvious mistakes, not a full JSON Schema implementation.
# bool is checked before int because bool is a subclass of int in Python, so
# {"type": "integer"} must not silently accept True.
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


@dataclass(frozen=True)
class ArgumentProblem:
    """One thing wrong with a call, in terms the caller can act on."""

    kind: str  # "unknown" | "missing_required" | "wrong_type"
    name: str
    detail: str


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    allowed = _TYPE_CHECKS.get(expected)
    if allowed is None:
        # An unknown/absent type keyword means the schema is not constraining
        # this field; do not invent a constraint it never declared.
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _expected_types(schema: Mapping[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in declared if isinstance(t, str)]
    return []


def validate_tool_arguments(
    spec: Any,
    arguments: Mapping[str, Any],
) -> list[ArgumentProblem]:
    """Return every problem with ``arguments``, or an empty list if the call is fine.

    ``spec`` is a ``ToolSpec``; it is typed loosely to keep this module free of
    an import cycle with the registry.

    A tool that declares no ``properties`` accepts anything on purpose (there are
    such tools), so no unknown-argument check is applied to it — otherwise this
    guard would break working calls while fixing a different bug.
    """
    parameters = getattr(spec, "parameters", None) or {}
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return []

    problems: list[ArgumentProblem] = []
    known = set(properties)

    for name in sorted(set(arguments) - known):
        problems.append(
            ArgumentProblem(
                kind="unknown",
                name=name,
                # Say plainly that nothing was stored. The whole failure this
                # guards against was a caller believing the value had been kept.
                detail=f"{name!r} is not a parameter of this tool and was NOT saved",
            )
        )

    required = parameters.get("required") or ()
    if isinstance(required, (list, tuple)):
        for name in required:
            if name not in arguments:
                problems.append(
                    ArgumentProblem(
                        kind="missing_required",
                        name=str(name),
                        detail=f"{name!r} is required",
                    )
                )

    for name, value in arguments.items():
        schema = properties.get(name)
        if not isinstance(schema, dict) or value is None:
            continue
        expected = _expected_types(schema)
        if expected and not any(_type_matches(value, t) for t in expected):
            problems.append(
                ArgumentProblem(
                    kind="wrong_type",
                    name=name,
                    detail=(
                        f"{name!r} expects {' or '.join(expected)}, "
                        f"got {type(value).__name__}"
                    ),
                )
            )

    return problems


def describe_problems(
    tool_name: str,
    problems: Iterable[ArgumentProblem],
    accepted: Iterable[str],
) -> dict[str, Any]:
    """Render problems as the standard error envelope body."""
    problems = list(problems)
    accepted_list = ", ".join(sorted(accepted)) or "(none)"
    unknown = [p.name for p in problems if p.kind == "unknown"]
    message = f"{tool_name}: " + "; ".join(p.detail for p in problems)
    hint = f"Accepted parameters: {accepted_list}."
    if unknown:
        # The concrete recovery, not just the rule. This is the exact mistake
        # that cost four task descriptions.
        hint += (
            " Values passed under an unknown name were discarded — re-send them"
            " under an accepted parameter, or use update_task for fields the"
            " create path does not take."
        )
    return {
        "code": "unknown_arguments" if unknown else "invalid_arguments",
        "message": message,
        "hint": hint,
        "problems": [
            {"kind": p.kind, "name": p.name, "detail": p.detail} for p in problems
        ],
    }
