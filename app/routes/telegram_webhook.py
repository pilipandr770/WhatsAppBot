"""Telegram Bot API webhook adapter.

Thin counterpart to the Evolution webhook: verify the request, parse a Telegram
Update into (contact_id, contact_name, text, is_voice), then hand off to the
shared channel-agnostic core in webhook.process_inbound.

Auth: Telegram echoes the secret_token we set with setWebhook in the
X-Telegram-Bot-Api-Secret-Token header. We compare it to the instance's stored
secret — no shared global token needed.
"""

import logging

from flask import Blueprint, request, jsonify

from app import db
from app.models import WhatsAppInstance
from app.services.telegram import telegram_client
from app.routes.webhook import process_inbound

telegram_bp = Blueprint('telegram_webhook', __name__)
logger = logging.getLogger(__name__)


@telegram_bp.route('/<instance_name>', methods=['POST'])
def handle_telegram(instance_name):
    instance = WhatsAppInstance.query.filter_by(
        instance_name=instance_name, channel='telegram'
    ).first()
    if not instance:
        return jsonify({'ok': True})  # unknown instance — ack and drop

    # Verify Telegram's secret_token header against the one we registered.
    provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if instance.telegram_webhook_secret and provided != instance.telegram_webhook_secret:
        logger.warning(f"Telegram webhook [{instance_name}]: bad secret token")
        return jsonify({'ok': True}), 200  # ack so Telegram doesn't retry; drop silently

    update = request.get_json(silent=True) or {}
    message = update.get('message') or {}
    if not message:
        return jsonify({'ok': True})

    chat = message.get('chat') or {}
    # Only handle 1:1 private chats (mirrors the WhatsApp group skip)
    if chat.get('type') != 'private':
        return jsonify({'ok': True})

    chat_id = str(chat.get('id', ''))
    if not chat_id:
        return jsonify({'ok': True})

    sender = message.get('from') or {}
    contact_name = (
        ' '.join(filter(None, [sender.get('first_name'), sender.get('last_name')])).strip()
        or sender.get('username')
        or chat_id
    )

    try:
        # Owner-linking deep link: /start link_<secret> captures the owner's chat_id
        # so booking/handoff notifications can reach them.
        text_raw = (message.get('text') or '').strip()
        if text_raw.startswith('/start') and _maybe_link_owner(instance, text_raw, chat_id):
            telegram_client.send_message(
                instance.api_token, chat_id,
                '✅ Benachrichtigungen aktiviert. Du erhältst hier ab sofort '
                'Infos zu neuen Terminen und Anfragen.'
            )
            return jsonify({'ok': True})

        text, is_voice = _extract_text(instance, message)
        if not text:
            return jsonify({'ok': True})

        process_inbound(instance, chat_id, contact_name, text, is_voice)
    except Exception as e:
        logger.error(f"Telegram webhook [{instance_name}]: {e}", exc_info=True)

    return jsonify({'ok': True})


def _maybe_link_owner(instance, text: str, chat_id: str) -> bool:
    """If this is the owner-link deep link, store the chat_id. Returns True if handled."""
    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ''
    expected = f"link_{instance.telegram_webhook_secret}"
    if payload and instance.telegram_webhook_secret and payload == expected:
        instance.owner_chat_id = chat_id
        db.session.commit()
        logger.info(f"Telegram owner linked for {instance.instance_name}: chat {chat_id}")
        return True
    return False


def _extract_text(instance, message: dict):
    """Return (text, is_voice). Transcribes voice notes via Telegram getFile."""
    text = (message.get('text') or '').strip()
    if text:
        return text, False

    # Voice note / audio → download and transcribe
    voice = message.get('voice') or message.get('audio') or {}
    file_id = voice.get('file_id')
    if file_id:
        audio_bytes, mime = telegram_client.download_file(instance.api_token, file_id)
        if audio_bytes:
            import base64
            from app.services.stt import transcribe_audio_base64
            b64 = base64.b64encode(audio_bytes).decode('ascii')
            transcript = transcribe_audio_base64(b64, mime or 'audio/ogg; codecs=opus')
            return (transcript or '').strip(), True

    # Caption on a photo/document the customer sent
    caption = (message.get('caption') or '').strip()
    return caption, False
