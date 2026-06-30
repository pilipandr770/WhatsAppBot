"""
Mistral provider — works for both Mistral Cloud API and self-hosted local models
(vLLM, Ollama, LM Studio) via OpenAI-compatible /v1/chat/completions endpoint.

mode='api'   → uses MISTRAL_API_KEY + https://api.mistral.ai/v1
mode='local' → uses LOCAL_LLM_URL  + no key required (or LOCAL_LLM_KEY if set)

Tool-calling: Mistral uses OpenAI-style function calling format.
We convert from the internal Anthropic-style tool schema and back.
"""
import os
import json
import logging
from typing import List, Dict, Optional, Callable

from .base import BaseLLMProvider

logger = logging.getLogger(__name__)

_SERVICE_UNAVAILABLE = (
    "Entschuldigung, unser KI-Service ist momentan nicht verfügbar. "
    "Bitte versuche es in ein paar Minuten erneut."
)

# Default models
_DEFAULT_API_MODEL   = 'mistral-small-latest'
_DEFAULT_LOCAL_MODEL = 'mistral'   # as registered in Ollama / vLLM


class MistralProvider(BaseLLMProvider):
    """
    Supports:
      - Mistral Cloud API  (mode='api')
      - Local / self-hosted (mode='local') — any OpenAI-compatible endpoint
    """

    def __init__(self, mode: str = 'api'):
        self.mode = mode

    # ------------------------------------------------------------------
    def _base_url(self):
        if self.mode == 'local':
            return os.environ.get('LOCAL_LLM_URL', 'http://localhost:11434/v1').rstrip('/')
        return 'https://api.mistral.ai/v1'

    def _api_key(self):
        if self.mode == 'local':
            return os.environ.get('LOCAL_LLM_KEY', 'local')  # Ollama ignores it
        return os.environ.get('MISTRAL_API_KEY', '')

    def _default_model(self):
        if self.mode == 'local':
            return os.environ.get('LOCAL_LLM_MODEL', _DEFAULT_LOCAL_MODEL)
        return os.environ.get('MISTRAL_MODEL', _DEFAULT_API_MODEL)

    def _headers(self):
        key = self._api_key()
        h = {'Content-Type': 'application/json'}
        if key and key != 'local':
            h['Authorization'] = f'Bearer {key}'
        return h

    # ------------------------------------------------------------------
    def get_ai_response(self, system_prompt, messages, rag_context=None,
                        max_tokens=500, model=None):
        import requests
        payload = {
            'model': model or self._default_model(),
            'max_tokens': max_tokens,
            'messages': _to_openai_messages(system_prompt, messages, rag_context),
        }
        try:
            resp = requests.post(
                f'{self._base_url()}/chat/completions',
                headers=self._headers(), json=payload, timeout=60,
            )
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content'] or ''
        except Exception as e:
            logger.error(f'[Mistral/{self.mode}] get_ai_response error: {e}')
            return _SERVICE_UNAVAILABLE

    # ------------------------------------------------------------------
    def get_ai_response_with_tools(self, system_prompt, messages, tools,
                                   tool_executor, rag_context=None,
                                   max_tokens=500, model=None):
        import requests
        m = model or self._default_model()
        oai_messages = _to_openai_messages(system_prompt, messages, rag_context)
        oai_tools = [_anthropic_tool_to_openai(t) for t in tools]

        for round_num in range(5):
            payload = {
                'model': m,
                'max_tokens': max_tokens,
                'messages': oai_messages,
                'tools': oai_tools,
                'tool_choice': 'auto',
            }
            try:
                resp = requests.post(
                    f'{self._base_url()}/chat/completions',
                    headers=self._headers(), json=payload, timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f'[Mistral/{self.mode}] round={round_num} error: {e}')
                return _SERVICE_UNAVAILABLE

            choice  = data['choices'][0]
            message = choice['message']
            finish  = choice.get('finish_reason', '')

            logger.debug(
                f'[Mistral/{self.mode}] round={round_num} finish={finish}'
            )

            if finish == 'stop' or not message.get('tool_calls'):
                return message.get('content') or ''

            if finish == 'tool_calls' or message.get('tool_calls'):
                # Append assistant message with tool_calls
                oai_messages.append(message)

                for tc in message.get('tool_calls', []):
                    fn = tc['function']
                    try:
                        args = json.loads(fn.get('arguments', '{}'))
                    except json.JSONDecodeError:
                        args = {}
                    logger.info(f'[Mistral/{self.mode}] tool={fn["name"]} args={args}')
                    result = tool_executor(fn['name'], args)
                    logger.info(f'[Mistral/{self.mode}] tool result: {result[:200]!r}')
                    oai_messages.append({
                        'role': 'tool',
                        'tool_call_id': tc['id'],
                        'content': result,
                    })
            else:
                logger.warning(f'[Mistral/{self.mode}] unexpected finish={finish}')
                return message.get('content') or ''

        logger.error(f'[Mistral/{self.mode}] max tool rounds reached')
        return "Es tut mir leid, ich konnte die Anfrage nicht abschließen."


# ---------------------------------------------------------------------------
# Format converters
# ---------------------------------------------------------------------------

def _to_openai_messages(system_prompt, messages, rag_context):
    """Build OpenAI-style message list with system prompt prepended."""
    sys_content = system_prompt
    if rag_context:
        sys_content += (
            "\n\n--- WISSENSDATENBANK ---\n"
            f"{rag_context}"
            "\n--- ENDE DER WISSENSDATENBANK ---\n\n"
            "Nutze die Informationen aus der Wissensdatenbank, wenn sie relevant sind."
        )
    result = [{'role': 'system', 'content': sys_content}]
    for msg in messages:
        if isinstance(msg.get('content'), str):
            result.append({'role': msg['role'], 'content': msg['content']})
    return result


def _anthropic_tool_to_openai(tool: dict) -> dict:
    """
    Convert Anthropic tool definition → OpenAI function tool format.

    Anthropic:
      {"name": "...", "description": "...", "input_schema": {"type": "object", "properties": {...}}}

    OpenAI:
      {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    return {
        'type': 'function',
        'function': {
            'name': tool['name'],
            'description': tool.get('description', ''),
            'parameters': tool.get('input_schema', {'type': 'object', 'properties': {}}),
        }
    }
