# Adding a New Agent

This is the step-by-step guide for adding a new agent to the AdForge
multi-agent graph.

## When to add an agent

Add a new agent when you have a **distinct persona** with its **own MCP
server** and its **own typed payload**. Examples that fit:
- "Brand voice" agent (own persona, no new MCP, takes CreativeConcept and
  rewrites copy in brand voice — but this is borderline, could be a
  Creative sub-tool).
- "Influencer sourcing" agent (own persona, own MCP for an influencer
  marketplace API, produces `InfluencerShortlist` payload — yes, add it).

Examples that don't fit:
- "Better headlines" — that's a Creative agent tool, not a new agent.
- "Send email summary" — that's an API endpoint, not an agent.

## The 5-step recipe

### 1. Create the feature folder

```
adforge/agents/<name>/
├── __init__.py     # exports YourAgent
├── persona.py      # the system prompt
├── agent.py        # the BaseAgent subclass
└── tools.py        # (optional) MCP tool wrappers
```

### 2. Write the persona

`persona.py`:

```python
PERSONA = """You are the <Name> Agent in AdForge.

Your role is to ... (one sentence).

You take ... and produce ... (typed payload).

You have access to MCP tools:
  - tool_name(args)  — what it does

RULES:
  - Constraint 1
  - Constraint 2
  - Constraint 3

You are <adjective>, <adjective>, <adjective>.
"""
```

The persona is the single biggest quality lever. Spend time on it.

### 3. Write the agent class

`agent.py`:

```python
from __future__ import annotations
import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.<name>.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, YourPayloadType


class YourAgent(BaseAgent):
    name = "<name>"           # must match the graph node id
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        upstream = json.dumps(agent_input.upstream_payloads, default=str)
        return [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": f"Brief:\n{brief}\n\nUpstream:\n{upstream}\n\n<instructions>"},
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        body = content.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1]
            if body.endswith("```"):
                body = body.rsplit("```", 1)[0]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            data = {"<fallback_field>": body[:500], "_parse_error": str(e)}
        # Validate against the typed contract
        validated = YourPayloadType(**data)
        return validated.model_dump()
```

### 4. Add the payload contract

Add a Pydantic model in `adforge/guardrails/contracts.py`:

```python
class YourPayloadType(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_a: str = Field(..., min_length=1, max_length=200)
    field_b: float = Field(..., ge=0)
    # ...
```

Export it from `adforge/guardrails/__init__.py`.

### 5. Wire into the graph

Edit `adforge/graph/state.py`:

1. Import your agent:
   ```python
   from adforge.agents.<name>.agent import YourAgent
   _your_agent = YourAgent()
   ```

2. Add a node function:
   ```python
   async def your_node(state: CampaignState) -> dict[str, Any]:
       result = await _your_agent.run({
           "request_id": state["request_id"],
           "tenant_id": state["tenant_id"],
           "campaign_id": state["campaign_id"],
           "agent_name": "<name>",
           "brief": state.get("brief"),
           "upstream_payloads": {"<needed_upstream_keys>": state.get("<key>", {})},
       })
       return {"<name>": result.output.payload} if result.output.error is None else {
           "<name>": {},
           "errors": state.get("errors", []) + [f"<name>: {result.output.error}"],
       }
   ```

3. Register + connect in `build_graph()`:
   ```python
   builder.add_node("<name>", your_node)
   builder.add_edge("<upstream_node>", "<name>")
   builder.add_edge("<name>", "<downstream_node>")
   ```

4. If your agent needs an MCP server, also:
   - Create `adforge/mcp_servers/<name>.py` with `mcp = FastMCP("adforge-<name>")`.
   - Register in `adforge/mcp_servers/runner.py` `SERVERS` dict.
   - Add a docker-compose service.
   - Add config keys + URL builder.
   - Register in `adforge/mcp_servers/client.py::_build_client`.

### 6. Add the field to `CampaignState`

In `adforge/graph/state.py`, add `<name>: dict[str, Any]` to the `CampaignState` TypedDict.

### 7. Write tests

Add a class to `tests/test_agents.py`:

```python
class TestYourAgent:
    def test_build_messages_includes_persona(self, sample_brief_dict):
        agent = YourAgent()
        inp = _make_input(sample_brief_dict)
        msgs = agent.build_messages(inp)
        assert msgs[0]["role"] == "system"
        assert "<Name> Agent" in msgs[0]["content"]

    def test_parse_response_validates_payload(self, sample_brief_dict):
        agent = YourAgent()
        inp = _make_input(sample_brief_dict)
        mock = {"field_a": "value", "field_b": 1.5}
        payload = agent.parse_response(json.dumps(mock), inp)
        assert payload["field_a"] == "value"
```

Add your agent to the eval suite in `adforge/utils/eval_runner.py` so CI
catches regressions.

### 8. Document

Add a row to the agents table in `README.md` and `docs/ARCHITECTURE.md`.

## Common pitfalls

- **Don't bypass `BaseAgent.run()`.** If you call the LLM directly, you
  lose the guardrails (audit, rate limit, tenant scope, persistence).
- **Don't put the agent's name in two places with different spellings.**
  The graph node id, `BaseAgent.name`, and the API listing in
  `adforge/api/main.py` must all match.
- **Don't forget `_make_input` in tests.** Tests need a valid
  `AgentInput` — use the helper in `tests/test_agents.py`.
- **Don't write a persona shorter than 200 words.** The persona is
  doing real work — it constrains the LLM's output format, encodes
  the rules, and tells it which MCP tools to call. Short personas
  produce vague agents.
- **Don't add `extra="allow"` to your payload model.** Use
  `extra="forbid"` — this catches typos in the LLM's output before
  they propagate downstream.
