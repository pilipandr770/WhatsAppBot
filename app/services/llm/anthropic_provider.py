"""Anthropic Claude provider — thin wrapper around the existing claude_service logic."""
import os
import logging
from typing import List, Dict, Optional, Callable

import anthropic

from .base import BaseLLMProvider

logger = logging.getLogger(__name__)

_SERVICE_UNAVAILABLE = (
    "Entschuldigung, unser KI-Service ist momentan nicht verfügbar. "
    "Bitte versuche es in ein paar Minuten erneut."
)
_DEFAULT_MODEL = 'claude-haiku-4-5-20251001'


class AnthropicProvider(BaseLLMProvider):

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=os.environ.get('ANTHROPIC_API_KEY', '')
            )
        return self._client

    # ------------------------------------------------------------------
    def get_ai_response(self, system_prompt, messages, rag_context=None,
                        max_tokens=500, model=None):
        full_system = _build_system(system_prompt, rag_context)
        msgs = _clean_messages(messages)
        m = model or os.environ.get('CLAUDE_MODEL', _DEFAULT_MODEL)
        try:
            resp = self._get_client().messages.create(
                model=m, max_tokens=max_tokens,
                system=full_system, messages=msgs,
            )
            return resp.content[0].text
        except anthropic.APIError as e:
            return _handle_error(e)

    # ------------------------------------------------------------------
    def get_ai_response_with_tools(self, system_prompt, messages, tools,
                                   tool_executor, rag_context=None,
                                   max_tokens=500, model=None):
        full_system = _build_system(system_prompt, rag_context)
        msgs = _clean_messages(messages)
        m = model or os.environ.get('CLAUDE_MODEL', _DEFAULT_MODEL)
        client = self._get_client()

        for round_num in range(5):
            try:
                resp = client.messages.create(
                    model=m, max_tokens=max_tokens,
                    system=full_system, messages=msgs, tools=tools,
                )
            except anthropic.APIError as e:
                return _handle_error(e)

            logger.debug(
                f"[Anthropic] round={round_num} stop={resp.stop_reason} "
                f"types={[b.type for b in resp.content]}"
            )

            if resp.stop_reason == 'end_turn':
                for b in resp.content:
                    if hasattr(b, 'text'):
                        return b.text
                return ''

            if resp.stop_reason == 'tool_use':
                tool_results = []
                for b in resp.content:
                    if b.type == 'tool_use':
                        logger.info(f"[Anthropic] tool={b.name} input={b.input}")
                        result = tool_executor(b.name, b.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": result,
                        })
                msgs.append({"role": "assistant", "content": resp.content})
                msgs.append({"role": "user", "content": tool_results})
            else:
                logger.warning(f"[Anthropic] unexpected stop={resp.stop_reason}")
                for b in resp.content:
                    if hasattr(b, 'text'):
                        return b.text
                break

        logger.error("[Anthropic] max tool rounds reached")
        return "Es tut mir leid, ich konnte die Anfrage nicht abschließen."


# ---------------------------------------------------------------------------

def _build_system(system_prompt, rag_context):
    s = system_prompt
    if rag_context:
        s += (
            "\n\n--- WISSENSDATENBANK ---\n"
            f"{rag_context}"
            "\n--- ENDE DER WISSENSDATENBANK ---\n\n"
            "Nutze die Informationen aus der Wissensdatenbank, wenn sie relevant sind."
        )
    return s


def _clean_messages(messages):
    if not messages:
        return [{"role": "user", "content": "Hallo"}]
    cleaned = []
    for msg in messages:
        if cleaned and cleaned[-1]['role'] == msg['role']:
            cleaned[-1]['content'] += '\n' + msg['content']
        else:
            cleaned.append({'role': msg['role'], 'content': msg['content']})
    if cleaned[0]['role'] != 'user':
        cleaned.insert(0, {'role': 'user', 'content': '...'})
    return cleaned


def _handle_error(err):
    txt = str(err)
    if 'credit balance is too low' in txt.lower():
        logger.error('Anthropic credits exhausted: %s', txt)
    else:
        logger.error('Anthropic API error: %s', txt)
    return _SERVICE_UNAVAILABLE
