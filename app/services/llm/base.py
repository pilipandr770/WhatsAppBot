"""
Abstract base for all LLM providers.

Internal message format follows Anthropic's convention:
  messages = [{"role": "user"|"assistant", "content": str}, ...]
  tools     = [{"name": str, "description": str, "input_schema": {...}}, ...]

Each provider translates this to its own wire format internally.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Callable


class BaseLLMProvider(ABC):

    @abstractmethod
    def get_ai_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        rag_context: Optional[str] = None,
        max_tokens: int = 500,
        model: Optional[str] = None,
    ) -> str:
        """Simple text response, no tools."""

    @abstractmethod
    def get_ai_response_with_tools(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
        tool_executor: Callable[[str, dict], str],
        rag_context: Optional[str] = None,
        max_tokens: int = 500,
        model: Optional[str] = None,
    ) -> str:
        """Agentic tool-use loop. Returns final text."""
