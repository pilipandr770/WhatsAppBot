"""Channel-agnostic messaging dispatch.

One place decides whether an instance talks over WhatsApp (Evolution API) or
Telegram (Bot API). Every outbound send in the app goes through here, so the
rest of the code (webhook core, media tools, product tools, owner
notifications) never has to know which channel an instance uses.

The `channel` attribute on WhatsAppInstance drives dispatch and defaults to
'whatsapp', so every pre-existing instance keeps its exact behaviour.
"""

import base64
import logging

logger = logging.getLogger(__name__)


def _channel(instance) -> str:
    return getattr(instance, 'channel', None) or 'whatsapp'


def send_text(instance, to_id: str, text: str) -> None:
    """Send a text reply to a customer on the instance's channel."""
    if _channel(instance) == 'telegram':
        from app.services.telegram import telegram_client
        telegram_client.send_message(instance.api_token, to_id, text)
    else:
        from app.services.evolution import evolution_client
        evolution_client.send_text(
            instance_name=instance.instance_name,
            token=instance.api_token,
            to_jid=to_id,
            text=text,
        )


def send_media(instance, to_id: str, media_bytes: bytes, kind: str,
               mimetype: str, filename: str, caption: str = '') -> None:
    """Send media (image/video/document) from raw bytes on the instance's channel.

    kind: 'image' | 'video' | 'document'
    """
    if _channel(instance) == 'telegram':
        from app.services.telegram import telegram_client
        telegram_client.send_media(
            token=instance.api_token,
            chat_id=to_id,
            media_bytes=media_bytes,
            kind=kind,
            mimetype=mimetype,
            filename=filename,
            caption=caption,
        )
    else:
        from app.services.evolution import evolution_client
        evolution_client.send_media(
            instance_name=instance.instance_name,
            token=instance.api_token,
            to_jid=to_id,
            media_b64=base64.b64encode(media_bytes).decode('ascii'),
            mediatype=kind,
            mimetype=mimetype,
            filename=filename,
            caption=caption,
        )


def owner_target(instance, config) -> str | None:
    """Resolve where owner notifications should go for this instance's channel.

    WhatsApp: the notification_phone from config (as a JID).
    Telegram: the owner_chat_id captured when the owner pressed /start.
    Returns None if the owner hasn't configured a destination.
    """
    if _channel(instance) == 'telegram':
        return getattr(instance, 'owner_chat_id', None) or None
    phone = getattr(config, 'notification_phone', None)
    return f"{phone}@s.whatsapp.net" if phone else None


def notify_owner(instance, config, text: str) -> bool:
    """Send an owner notification on the right channel. Returns True if sent."""
    target = owner_target(instance, config)
    if not target:
        return False
    try:
        send_text(instance, target, text)
        return True
    except Exception as e:
        logger.warning(f"notify_owner failed on {instance.instance_name}: {e}")
        return False
