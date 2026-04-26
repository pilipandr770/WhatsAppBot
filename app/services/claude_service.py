import anthropic
import os
import logging
from typing import List, Dict, Optional, Callable

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
    Get AI response from Claude (no tool use).
    messages: [{"role": "user"|"assistant", "content": "..."}]
    """
    full_system = _build_system(system_prompt, rag_context)
    cleaned_messages = _clean_messages(messages)

    client = get_client()
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        system=full_system,
        messages=cleaned_messages
    )

    return response.content[0].text


def get_ai_response_with_tools(
    system_prompt: str,
    messages: List[Dict[str, str]],
    tools: List[Dict],
    tool_executor: Callable[[str, dict], str],
    rag_context: Optional[str] = None,
    max_tokens: int = 500
) -> str:
    """
    Get AI response from Claude with tool use support.

    Runs an agentic loop:
      1. Send messages + tools to Claude.
      2. If Claude calls a tool → execute it, feed result back, repeat.
      3. Return the final text response.

    tool_executor(tool_name: str, tool_input: dict) -> str
    """
    full_system = _build_system(system_prompt, rag_context)
    cleaned_messages = _clean_messages(messages)
    client = get_client()

    # Safety: max 5 tool-call rounds to prevent infinite loops
    for round_num in range(5):
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=max_tokens,
            system=full_system,
            messages=cleaned_messages,
            tools=tools
        )

        logger.debug(
            f"[Claude tools] round={round_num} stop_reason={response.stop_reason} "
            f"content_types={[b.type for b in response.content]}"
        )

        if response.stop_reason == 'end_turn':
            # Find and return the text block
            for block in response.content:
                if hasattr(block, 'text'):
                    return block.text
            return ''

        elif response.stop_reason == 'tool_use':
            # Execute each requested tool
            tool_result_blocks = []
            for block in response.content:
                if block.type == 'tool_use':
                    logger.info(f"[Claude tools] Calling tool={block.name} input={block.input}")
                    result_text = tool_executor(block.name, block.input)
                    logger.info(f"[Claude tools] Tool {block.name} result: {result_text[:200]!r}")
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            # Append assistant's tool-use turn, then user's tool results
            cleaned_messages.append({
                "role": "assistant",
                "content": response.content,  # SDK accepts ContentBlock list here
            })
            cleaned_messages.append({
                "role": "user",
                "content": tool_result_blocks,
            })

        else:
            # Unexpected stop reason — return whatever text we have
            logger.warning(f"[Claude tools] Unexpected stop_reason={response.stop_reason}")
            for block in response.content:
                if hasattr(block, 'text'):
                    return block.text
            break

    logger.error("[Claude tools] Reached max tool-call rounds without end_turn")
    return "Es tut mir leid, ich konnte die Anfrage nicht abschließen."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_system(system_prompt: str, rag_context: Optional[str]) -> str:
    full_system = system_prompt
    if rag_context:
        full_system += (
            "\n\n--- WISSENSDATENBANK ---\n"
            f"{rag_context}"
            "\n--- ENDE DER WISSENSDATENBANK ---\n\n"
            "Nutze die Informationen aus der Wissensdatenbank, wenn sie für die Frage relevant sind. "
            "Antworte immer basierend auf den verfügbaren Informationen."
        )
    return full_system


def _clean_messages(messages: List[Dict]) -> List[Dict]:
    """Ensure valid alternating user/assistant pattern for Anthropic API."""
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
