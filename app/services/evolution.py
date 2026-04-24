import requests
import secrets
import os
import logging

logger = logging.getLogger(__name__)


class EvolutionAPIClient:
    def __init__(self):
        self.base_url = os.environ.get('EVOLUTION_API_URL', 'http://evolution-api:8080').rstrip('/')
        self.global_key = os.environ.get('EVOLUTION_API_KEY', '')
        self.app_base_url = os.environ.get('APP_BASE_URL', 'https://yourdomain.com')

    def _headers(self, token=None):
        return {
            'Content-Type': 'application/json',
            'apikey': token or self.global_key
        }

    def create_instance(self, instance_name: str) -> tuple[dict, str]:
        """Create Evolution API instance. Returns (response, token)."""
        token = secrets.token_urlsafe(32)
        webhook_url = f"{self.app_base_url}/wh/{instance_name}"

        payload = {
            'instanceName': instance_name,
            'token': token,
            'qrcode': True,
            'integration': 'WHATSAPP-BAILEYS',
            'webhook': {
                'enabled': True,
                'url': webhook_url,
                'byEvents': False,
                'base64': True,
                'events': [
                    'MESSAGES_UPSERT',
                    'CONNECTION_UPDATE',
                    'QRCODE_UPDATED'
                ]
            }
        }

        resp = requests.post(
            f'{self.base_url}/instance/create',
            json=payload,
            headers=self._headers(),
            timeout=45
        )
        resp.raise_for_status()
        return resp.json(), token

    def get_qr(self, instance_name: str, token: str) -> dict:
        """Get QR code for connecting WhatsApp."""
        resp = requests.get(
            f'{self.base_url}/instance/connect/{instance_name}',
            headers=self._headers(token),
            timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def trigger_connect(self, instance_name: str, token: str) -> None:
        """Fire-and-forget: start WhatsApp connection to trigger QR generation via webhook."""
        try:
            requests.get(
                f'{self.base_url}/instance/connect/{instance_name}',
                headers=self._headers(token),
                timeout=30
            )
        except Exception as e:
            logger.debug(f"trigger_connect {instance_name}: {e}")

    def get_connection_state(self, instance_name: str, token: str) -> str:
        """Returns: open | connecting | close"""
        try:
            resp = requests.get(
                f'{self.base_url}/instance/connectionState/{instance_name}',
                headers=self._headers(token),
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get('instance', {}).get('state', 'close')
        except Exception as e:
            logger.error(f"get_connection_state error: {e}")
        return 'close'

    def send_text(self, instance_name: str, token: str, to_jid: str, text: str) -> dict:
        """Send text message. to_jid can be phone@s.whatsapp.net or just phone number."""
        number = to_jid.replace('@s.whatsapp.net', '').replace('@g.us', '')

        payload = {
            'number': number,
            'text': text
        }

        resp = requests.post(
            f'{self.base_url}/message/sendText/{instance_name}',
            json=payload,
            headers=self._headers(token),
            timeout=20
        )
        resp.raise_for_status()
        return resp.json()

    def delete_instance(self, instance_name: str, token: str) -> bool:
        """Disconnect and delete instance."""
        try:
            resp = requests.delete(
                f'{self.base_url}/instance/delete/{instance_name}',
                headers=self._headers(token),
                timeout=10
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"delete_instance error: {e}")
            return False

    def logout_instance(self, instance_name: str, token: str) -> bool:
        """Logout WhatsApp session (keeps instance)."""
        try:
            resp = requests.delete(
                f'{self.base_url}/instance/logout/{instance_name}',
                headers=self._headers(token),
                timeout=10
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False


evolution_client = EvolutionAPIClient()
