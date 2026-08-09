"""Calendar owner notifications must dispatch on the instance's own channel."""

from unittest.mock import MagicMock, patch

from app.services import calendar_service as cs


class _Inst:
    def __init__(self, channel, api_token='tok', instance_name='wa_1', owner_chat_id=None):
        self.channel = channel
        self.api_token = api_token
        self.instance_name = instance_name
        self.owner_chat_id = owner_chat_id
        self.id = 1


class _Cfg:
    def __init__(self, notification_phone=None):
        self.notification_phone = notification_phone


def test_calendar_notify_telegram_uses_bot_api():
    tg = _Inst('telegram', api_token='123:ABC', owner_chat_id='7444992311')
    with patch('app.services.telegram.telegram_client') as tgc, \
         patch('app.services.evolution.evolution_client') as evo:
        cs._notify_owner(tg, _Cfg('4916095030120'), 'Neuer Termin gebucht')
        assert tgc.send_message.called
        assert not evo.send_text.called
        # Sent to the linked owner chat, not the notification_phone
        assert '7444992311' in str(tgc.send_message.call_args)


def test_calendar_notify_whatsapp_uses_evolution():
    wa = _Inst('whatsapp')
    with patch('app.services.telegram.telegram_client') as tgc, \
         patch('app.services.evolution.evolution_client') as evo:
        cs._notify_owner(wa, _Cfg('4917000'), 'Neuer Termin gebucht')
        assert evo.send_text.called
        assert not tgc.send_message.called


def test_calendar_notify_noop_without_target():
    tg = _Inst('telegram', owner_chat_id=None)
    with patch('app.services.telegram.telegram_client') as tgc, \
         patch('app.services.evolution.evolution_client') as evo:
        cs._notify_owner(tg, _Cfg(None), 'x')
        assert not tgc.send_message.called
        assert not evo.send_text.called
