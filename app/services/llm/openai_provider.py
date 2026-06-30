"""
OpenAI provider — GPT-4o / GPT-4o-mini via OpenAI API.
Uses the same OpenAI-compatible format as MistralProvider.
"""
import os
import json
import logging
from typing import List, Dict, Optional, Callable

from .base import BaseLLMProvider
from .mistral_provider import _to_openai_messages, _anthropic_tool_to_openai

logger = logging.getLogger(__name__)

_SERVICE_UNAVAILABLE = (
    "Entschuldigung, unser KI-Service ist momentan nicht verfügbar. "
    "Bitte versuche es in ein paar Minuten erneut."
)
_DEFAULT_MODEL = 'gpt-4o-mini'


class OpenAIProvider(BaseLLMProvider):

    def _api_key(self):
        return os.environ.get('OPENAI_API_KEY', '')

    def _default_model(self):
        return os.environ.get('OPENAI_CHAT_MODEL', _DEFAULT_MODEL)

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self._api_key()}',
        }

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
                'https://api.openai.com/v1/chat/completions',
                headers=self._headers(), json=payload, timeout=60,
            )
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content'] or ''
        except Exception as e:
            logger.error(f'[OpenAI] get_ai_response error: {e}')
            return _SERVICE_UNAVAILABLE

    # ------------------------------------------------------------------
    def get_ai_response_with_tools(self, system_prompt, messages, tools,
                                   tool_executor, rag_context=None,
                                   max_tokens=500, model=None):
        import requests
        m = model or self._default_model()
        oai_messages = _to_openai_messages(system_prompt, messages, rag_context)
        oai_tools    = [_anthropic_tool_to_openai(t) for t in tools]

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
                    'https://api.openai.com/v1/chat/completions',
                    headers=self._headers(), json=payload, timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f'[OpenAI] round={round_num} error: {e}')
                return _SERVICE_UNAVAILABLE

            choice  = data['choices'][0]
            message = choice['message']
            finish  = choice.get('finish_reason', '')

            logger.debug(f'[OpenAI] round={round_num} finish={finish}')

            if finish == 'stop' or not message.get('tool_calls'):
                return message.get('content') or ''

            oai_messages.append(message)
            for tc in message.get('tool_calls', []):
                fn = tc['function']
                try:
                    args = json.loads(fn.get('arguments', '{}'))
                except json.JSONDecodeError:
                    args = {}
                logger.info(f'[OpenAI] tool={fn["name"]} args={args}')
                result = tool_executor(fn['name'], args)
                oai_messages.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': result,
                })

        logger.error('[OpenAI] max tool rounds reached')
        return "Es tut mir leid, ich konnte die Anfrage nicht abschließen."
