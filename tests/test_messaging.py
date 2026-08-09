"""Channel dispatch: messaging routes to Evolution (WhatsApp) or Telegram Bot API."""

from unittest.mock import patch

from app.services import messaging


class _Inst:
    def __init__(self, channel, api_token='tok', instance_name='wa_1', owner_chat_id=None):
        self.channel = channel
        self.api_token = api_token
        self.instance_name = instance_name
        self.owner_chat_id = owner_chat_id


class _Cfg:
    def __init__(self, notification_phone=None):
        self.notification_phone = notification_phone


def test_send_text_whatsapp_uses_evolution():
    inst = _Inst('whatsapp')
    with patch('app.services.evolution.evolution_client') as evo, \
         patch('app.services.telegram.telegram_client') as tg:
        messaging.send_text(inst, '49150@s.whatsapp.net', 'hi')
        assert evo.send_text.called
        assert not tg.send_message.called


def test_send_text_telegram_uses_bot_api():
    inst = _Inst('telegram', api_token='123:ABC')
    with patch('app.services.evolution.evolution_client') as evo, \
         patch('app.services.telegram.telegram_client') as tg:
        messaging.send_text(inst, '555', 'hi')
        assert tg.send_message.called
        assert not evo.send_text.called


def test_send_media_encodes_for_evolution_but_raw_for_telegram():
    wa = _Inst('whatsapp')
    tg = _Inst('telegram', api_token='123:ABC')
    with patch('app.services.evolution.evolution_client') as evo, \
         patch('app.services.telegram.telegram_client') as tgc:
        messaging.send_media(wa, 'p', b'xx', 'image', 'image/png', 'a.png', 'cap')
        messaging.send_media(tg, '555', b'xx', 'image', 'image/png', 'a.png', 'cap')
        # Evolution gets base64 string, Telegram gets raw bytes
        assert isinstance(evo.send_media.call_args.kwargs['media_b64'], str)
        assert isinstance(tgc.send_media.call_args.kwargs['media_bytes'], bytes)


def test_owner_target_per_channel():
    wa = _Inst('whatsapp')
    tg = _Inst('telegram', owner_chat_id='555')
    tg_unlinked = _Inst('telegram', owner_chat_id=None)
    assert messaging.owner_target(wa, _Cfg('4917000')) == '4917000@s.whatsapp.net'
    assert messaging.owner_target(wa, _Cfg(None)) is None
    assert messaging.owner_target(tg, _Cfg('ignored')) == '555'
    assert messaging.owner_target(tg_unlinked, _Cfg('ignored')) is None


def test_notify_owner_returns_false_without_target():
    tg_unlinked = _Inst('telegram', owner_chat_id=None)
    with patch('app.services.telegram.telegram_client') as tg:
        assert messaging.notify_owner(tg_unlinked, _Cfg(None), 'x') is False
        assert not tg.send_message.called
