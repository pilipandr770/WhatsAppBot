"""
Speech-to-Text service.

Provider priority (first key found wins):
  1. GROQ_API_KEY  → Groq Whisper (whisper-large-v3-turbo) — fast & cheap
  2. OPENAI_API_KEY → OpenAI Whisper (whisper-1)            — fallback
"""
import base64
import io
import os
import logging

import requests

logger = logging.getLogger(__name__)

# Whisper supports these extensions
_MIME_TO_EXT = {
    'audio/ogg': 'ogg',
    'audio/mpeg': 'mp3',
    'audio/mp4': 'mp4',
    'audio/wav': 'wav',
    'audio/webm': 'webm',
    'audio/x-m4a': 'm4a',
}

# Provider configs
_PROVIDERS = [
    {
        'name':    'Groq',
        'env':     'GROQ_API_KEY',
        'url':     'https://api.groq.com/openai/v1/audio/transcriptions',
        'model':   'whisper-large-v3-turbo',
    },
    {
        'name':    'OpenAI',
        'env':     'OPENAI_API_KEY',
        'url':     'https://api.openai.com/v1/audio/transcriptions',
        'model':   'whisper-1',
    },
]


def transcribe_audio_base64(audio_base64: str, mimetype: str = 'audio/ogg') -> str:
    """
    Transcribe base64-encoded audio.
    Tries Groq first (if GROQ_API_KEY set), falls back to OpenAI Whisper.
    Returns transcribed text or empty string.
    """
    if not audio_base64:
        return ''

    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as e:
        logger.error(f'STT: base64 decode error: {e}')
        return ''

    clean_mime = mimetype.split(';')[0].strip().lower()
    ext = _MIME_TO_EXT.get(clean_mime, 'ogg')

    for provider in _PROVIDERS:
        api_key = os.environ.get(provider['env'], '').strip()
        if not api_key:
            continue

        logger.info(
            f"STT: transcribing {len(audio_bytes)} bytes ({clean_mime}) "
            f"via {provider['name']} ({provider['model']})"
        )
        try:
            resp = requests.post(
                provider['url'],
                headers={'Authorization': f"Bearer {api_key}"},
                files={
                    'file':  (f'voice.{ext}', io.BytesIO(audio_bytes), mimetype),
                    'model': (None, provider['model']),
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json().get('text', '').strip()
            logger.info(
                f"STT ({provider['name']}): transcribed {len(text)} chars: {text[:80]!r}"
            )
            return text

        except requests.HTTPError as e:
            logger.error(
                f"STT ({provider['name']}): HTTP {e.response.status_code}: "
                f"{e.response.text[:200]}"
            )
        except Exception as e:
            logger.error(f"STT ({provider['name']}): unexpected error: {e}")

    logger.warning('STT: no API key configured (set GROQ_API_KEY or OPENAI_API_KEY)')
    return ''


def transcribe_from_evolution(instance_name: str, token: str, message_id: str,
                               evolution_base_url: str, evolution_key: str,
                               remote_jid: str = '') -> str:
    """
    Fallback: ask Evolution API to download & encode the media, then transcribe.
    Used when webhook payload doesn't include base64 audio directly (v2.3.x).
    """
    api_key = token or evolution_key
    url = f'{evolution_base_url}/chat/getBase64FromMediaMessage/{instance_name}'
    body = {
        'message': {
            'key': {
                'id': message_id,
                'remoteJid': remote_jid,
                'fromMe': False,
            }
        },
        'convertToMp4': False,
    }
    logger.info(f'STT fallback: POST {url} key_id={message_id}')
    try:
        resp = requests.post(url, json=body, headers={'apikey': api_key}, timeout=25)
        logger.info(f'STT fallback: status={resp.status_code} body={resp.text[:300]}')
        if resp.status_code >= 400:
            return ''
        data = resp.json()
        b64 = data.get('base64', '')
        mime = data.get('mimetype', 'audio/ogg; codecs=opus')
        if b64:
            logger.info(f'STT fallback: got {len(b64)} base64 chars, mime={mime}')
            return transcribe_audio_base64(b64, mime)
        else:
            logger.warning(f'STT fallback: no base64 in response. keys={list(data.keys())}')
    except Exception as e:
        logger.error(f'STT fallback: error: {e}')
    return ''
