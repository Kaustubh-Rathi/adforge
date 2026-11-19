"""Shared JSON extraction + contract validation for agent responses."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from adforge.guardrails.exceptions import ContractValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


def strip_code_fence(content: str) -> str:
    """Remove a surrounding Markdown code fence, if present."""
    body = content.strip()
    if body.startswith("```"):
        # Drop the opening fence line (```json / ```).
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    return body.strip()


def extract_json(content: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM reply.

    Handles fenced blocks, leading prose, and trailing prose. Raises
    ``ContractValidationError`` when no parseable object exists.
    """
    body = strip_code_fence(content)

    candidates = [body]
    start = body.find("{")
    end = body.rfind("}")
    if start != -1 and end > start:
        candidates.append(body[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data

    raise ContractValidationError(
        agent_name="response",
        errors=[{"type": "json_decode", "message": "no JSON object in response"}],
    )


def validate_payload(
    model: type[ModelT],
    data: dict[str, Any],
    agent_name: str,
) -> dict[str, Any]:
    """Validate ``data`` against a Pydantic model, raising a typed error."""
    try:
        return model(**data).model_dump(mode="json")
    except ValidationError as exc:
        raise ContractValidationError(
            agent_name=agent_name,
            errors=[dict(err) for err in exc.errors(include_url=False)],
        ) from exc


def schema_instruction(model: type[BaseModel], *, array_key: str | None = None) -> str:
    """Prompt fragment that pins the exact output schema for the LLM.

    Small/free models frequently invent extra keys or change nested shapes when
    only told a schema *name*. Supplying the real JSON Schema (plus the wrapper
    shape for list outputs) makes first-pass validation much more reliable and
    keeps the one repair round available for genuine mistakes.
    """
    schema = json.dumps(model.model_json_schema(), indent=2)
    target = (
        f'{{"{array_key}": [<object matching the schema>, ...]}}'
        if array_key
        else "<object matching the schema>"
    )
    return (
        "\n\nRespond with ONLY one JSON value — no prose, no code fence. "
        "It MUST validate against this JSON Schema. Use exactly the declared "
        "types and do not add undeclared keys.\n"
        f"Target shape: {target}\n"
        f"JSON Schema:\n{schema}"
    )
