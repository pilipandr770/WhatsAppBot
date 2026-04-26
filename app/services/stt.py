"""
Speech-to-Text service using OpenAI Whisper API.
Transcribes WhatsApp voice messages (ogg/opus) to text.
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


def transcribe_audio_base64(audio_base64: str, mimetype: str = 'audio/ogg') -> str:
    """
    Transcribe base64-encoded audio using OpenAI Whisper API.

    Returns transcribed text, or empty string if STT is unavailable/failed.
    """
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        logger.warning('STT: OPENAI_API_KEY not set — cannot transcribe voice message')
        return ''

    if not audio_base64:
        return ''

    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as e:
        logger.error(f'STT: base64 decode error: {e}')
        return ''

    # Determine file extension — Whisper needs a filename with valid extension
    clean_mime = mimetype.split(';')[0].strip().lower()  # strip codecs param
    ext = _MIME_TO_EXT.get(clean_mime, 'ogg')

    logger.info(f'STT: transcribing {len(audio_bytes)} bytes ({clean_mime}) via Whisper')

    try:
        resp = requests.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {api_key}'},
            files={
                'file': (f'voice.{ext}', io.BytesIO(audio_bytes), mimetype),
                'model': (None, 'whisper-1'),
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json().get('text', '').strip()
        logger.info(f'STT: transcribed {len(text)} chars: {text[:80]!r}')
        return text

    except requests.HTTPError as e:
        logger.error(f'STT: Whisper API error {e.response.status_code}: {e.response.text[:200]}')
    except Exception as e:
        logger.error(f'STT: unexpected error: {e}')

    return ''


def transcribe_from_evolution(instance_name: str, token: str, message_id: str,
                               evolution_base_url: str, evolution_key: str) -> str:
    """
    Fallback: ask Evolution API to download & encode the media, then transcribe.
    Used when webhook payload doesn't include base64 audio directly.
    """
    try:
        resp = requests.post(
            f'{evolution_base_url}/chat/getBase64FromMediaMessage/{instance_name}',
            json={'message': {'key': {'id': message_id}}, 'convertToMp4': False},
            headers={'apikey': token or evolution_key},
            timeout=20,
        )
        if resp.status_code != 200:
            return ''
        data = resp.json()
        b64 = data.get('base64', '')
        mime = data.get('mimetype', 'audio/ogg')
        if b64:
            return transcribe_audio_base64(b64, mime)
    except Exception as e:
        logger.error(f'STT: evolution media download error: {e}')
    return ''
