"""Telegram Bot API client.

Mirrors the shape of evolution.py so the messaging layer can dispatch to
either backend. Telegram is the official Bot API — no QR, no session to keep
alive, connection handled by Telegram. The customer's bot token (from
@BotFather) is stored in WhatsAppInstance.api_token; the per-instance webhook
is secured with a secret_token echoed back by Telegram in a request header.
"""

import io
import logging
import os

import requests

logger = logging.getLogger(__name__)

API_ROOT = 'https://api.telegram.org'


class TelegramAPIClient:
    def __init__(self):
        self.app_base_url = os.environ.get('APP_BASE_URL', 'https://whatsappbothelfer.de').rstrip('/')

    def _api(self, token: str, method: str) -> str:
        return f'{API_ROOT}/bot{token}/{method}'

    def get_me(self, token: str) -> dict | None:
        """Validate a bot token. Returns the bot info dict, or None if invalid."""
        try:
            resp = requests.get(self._api(token, 'getMe'), timeout=15)
            data = resp.json()
            if resp.status_code == 200 and data.get('ok'):
                return data['result']
            logger.warning(f"getMe rejected token: {data.get('description')}")
            return None
        except Exception as e:
            logger.error(f"getMe error: {e}")
            return None

    def set_webhook(self, token: str, instance_name: str, secret_token: str) -> bool:
        """Register the per-instance webhook with Telegram."""
        url = f'{self.app_base_url}/tg/{instance_name}'
        try:
            resp = requests.post(
                self._api(token, 'setWebhook'),
                json={
                    'url': url,
                    'secret_token': secret_token,
                    'allowed_updates': ['message'],
                    'drop_pending_updates': True,
                },
                timeout=15,
            )
            data = resp.json()
            ok = bool(data.get('ok'))
            logger.info(f"setWebhook {instance_name}: ok={ok} desc={data.get('description')}")
            return ok
        except Exception as e:
            logger.error(f"setWebhook {instance_name}: {e}")
            return False

    def delete_webhook(self, token: str) -> bool:
        try:
            resp = requests.post(
                self._api(token, 'deleteWebhook'),
                json={'drop_pending_updates': False},
                timeout=15,
            )
            return bool(resp.json().get('ok'))
        except Exception as e:
            logger.error(f"deleteWebhook: {e}")
            return False

    def send_message(self, token: str, chat_id, text: str) -> dict:
        """Send a text message. Telegram caps text at 4096 chars per message."""
        resp = requests.post(
            self._api(token, 'sendMessage'),
            json={'chat_id': chat_id, 'text': text[:4096]},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def send_media(self, token: str, chat_id, media_bytes: bytes, kind: str,
                   mimetype: str, filename: str, caption: str = '') -> dict:
        """Send a photo/video/document from raw bytes via multipart upload.

        kind: 'image' | 'video' | 'document' (same vocabulary as evolution.send_media).
        """
        if kind == 'image':
            method, field = 'sendPhoto', 'photo'
        elif kind == 'video':
            method, field = 'sendVideo', 'video'
        else:
            method, field = 'sendDocument', 'document'

        files = {field: (filename or 'file', io.BytesIO(media_bytes), mimetype or 'application/octet-stream')}
        payload = {'chat_id': str(chat_id)}
        if caption:
            payload['caption'] = caption[:1024]

        resp = requests.post(self._api(token, method), data=payload, files=files, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def download_file(self, token: str, file_id: str) -> tuple[bytes, str] | tuple[None, None]:
        """Resolve a file_id to (bytes, mime_hint). Used for voice transcription."""
        try:
            r = requests.get(self._api(token, 'getFile'), params={'file_id': file_id}, timeout=15)
            data = r.json()
            if not data.get('ok'):
                return None, None
            file_path = data['result']['file_path']
            dl = requests.get(f'{API_ROOT}/file/bot{token}/{file_path}', timeout=30)
            dl.raise_for_status()
            # Telegram voice notes are always OGG/Opus
            return dl.content, 'audio/ogg; codecs=opus'
        except Exception as e:
            logger.error(f"download_file {file_id}: {e}")
            return None, None


telegram_client = TelegramAPIClient()
