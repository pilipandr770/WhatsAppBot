import anthropic
import os
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    return _client


def get_ai_response(
    system_prompt: str,
    messages: List[Dict[str, str]],
    rag_context: Optional[str] = None,
    max_tokens: int = 500
) -> str:
    """
    Get AI response from Claude.
    messages: [{"role": "user"|"assistant", "content": "..."}]
    """
    full_system = system_prompt

    if rag_context:
        full_system += (
            "\n\n--- WISSENSDATENBANK ---\n"
            f"{rag_context}"
            "\n--- ENDE DER WISSENSDATENBANK ---\n\n"
            "Nutze die Informationen aus der Wissensdatenbank, wenn sie für die Frage relevant sind. "
            "Antworte immer basierend auf den verfügbaren Informationen."
        )

    # Ensure alternating roles (Anthropic requirement)
    cleaned_messages = _clean_messages(messages)

    client = get_client()
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        system=full_system,
        messages=cleaned_messages
    )

    return response.content[0].text


def _clean_messages(messages: List[Dict]) -> List[Dict]:
    """Ensure valid alternating user/assistant pattern."""
    if not messages:
        return [{"role": "user", "content": "Hallo"}]

    cleaned = []
    for msg in messages:
        if cleaned and cleaned[-1]['role'] == msg['role']:
            # Merge consecutive same-role messages
            cleaned[-1]['content'] += '\n' + msg['content']
        else:
            cleaned.append({'role': msg['role'], 'content': msg['content']})

    # Must start with user
    if cleaned[0]['role'] != 'user':
        cleaned.insert(0, {'role': 'user', 'content': '...'})

    return cleaned
