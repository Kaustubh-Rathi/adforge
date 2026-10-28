"""LangChain-compatible wrapper around our LLMProvider.

LangChain's ``create_agent`` expects a model that implements
``BaseChatModel.bind_tools(tools)`` and returns AIMessage objects. Our
``LLMProvider`` is a simpler async interface.

This wrapper bridges the two so we can use ``create_agent`` with the
Z.ai GLM provider (or any other provider we add).
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from adforge.llm.base import LLMProvider


class LangChainLLMWrapper(BaseChatModel):
    """Minimal adapter so our LLMProvider works with LangChain's create_agent."""

    provider: LLMProvider

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "adforge-llm-wrapper"

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        # Synchronous fallback — ReAct agents are async, so this is rarely called.
        import asyncio
        return asyncio.run(self._agenerate_internal(messages, **kwargs))

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return await self._agenerate_internal(messages, **kwargs)

    async def _agenerate_internal(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        # Convert LangChain messages to our chat format
        chat_messages = []
        for m in messages:
            if isinstance(m, SystemMessage):
                chat_messages.append({"role": "system", "content": m.content})
            elif isinstance(m, HumanMessage):
                chat_messages.append({"role": "user", "content": m.content})
            elif isinstance(m, AIMessage):
                chat_messages.append({"role": "assistant", "content": m.content})
            else:
                chat_messages.append({"role": "user", "content": str(m.content)})

        # Extract tools in OpenAI function-calling format
        tools_spec = kwargs.get("tools") or self._bound_tools

        response = await self.provider.chat(
            chat_messages,
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
            tools=tools_spec,
        )

        # Parse tool calls if the provider returned any
        ai_message = AIMessage(content=response.content)
        if response.raw.get("tool_calls"):
            ai_message.additional_kwargs["tool_calls"] = response.raw["tool_calls"]

        generation = ChatGeneration(message=ai_message)
        return ChatResult(generations=[generation])

    def bind_tools(self, tools: list, **kwargs: Any) -> "LangChainLLMWrapper":
        """Store the tool schemas for the next call.

        The actual tool dispatch is handled by LangGraph's ToolNode — the
        LLM just needs to know the schemas so it can emit tool_call dicts.
        """
        # Convert LangChain Tool objects to OpenAI function schemas
        tool_schemas = []
        for t in tools:
            if hasattr(t, "args_schema"):
                tool_schemas.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.args_schema.model_json_schema() if t.args_schema else {},
                    },
                })
            elif isinstance(t, dict):
                tool_schemas.append(t)
        # Return a new wrapper with the tools bound
        bound = LangChainLLMWrapper(provider=self.provider)
        bound._bound_tools = tool_schemas  # type: ignore[attr-defined]
        return bound

    _bound_tools: list = Field(default_factory=list, exclude=True)
