"""
LLM provider factory.

Usage:
    from app.services.llm import get_provider
    provider = get_provider('mistral')
    text = provider.get_ai_response(system, messages, max_tokens=500)
"""
from .anthropic_provider import AnthropicProvider
from .mistral_provider import MistralProvider
from .openai_provider import OpenAIProvider

_PROVIDERS = {
    'anthropic': AnthropicProvider(),
    'mistral':   MistralProvider(mode='api'),
    'local':     MistralProvider(mode='local'),   # same code, different base_url
    'openai':    OpenAIProvider(),
}


def get_provider(name: str):
    """Return the LLM provider instance for the given name. Defaults to Anthropic."""
    return _PROVIDERS.get(name or 'anthropic', _PROVIDERS['anthropic'])
