"""LLM provider abstraction.

Default: Z.ai GLM (https://z.ai). Adapter layer so users can swap to OpenAI or
Anthropic via env var LLM_PROVIDER without touching agent code.

All agents consume ``get_llm()`` and get a configured chat model with
temperature + max_tokens set from settings. Rate limiting + audit logging
happen in the BaseAgent, not here.
"""

from adforge.llm.factory import get_llm, LLMProvider
from adforge.llm.base import LLMResponse, LLMUsage

__all__ = ["get_llm", "LLMProvider", "LLMResponse", "LLMUsage"]
