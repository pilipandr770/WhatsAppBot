"""Telegram webhook adapter: owner-linking deep link, text/voice/caption parsing."""

from unittest.mock import MagicMock, patch

from app.routes import telegram_webhook as tw


class _Inst:
    def __init__(self, secret='SEC', token='123:ABC', name='tg_1'):
        self.telegram_webhook_secret = secret
        self.api_token = token
        self.instance_name = name
        self.owner_chat_id = None


def test_owner_link_deep_link_sets_chat_id():
    inst = _Inst()
    with patch.object(tw, 'db') as db:
        assert tw._maybe_link_owner(inst, '/start link_SEC', '999') is True
        assert inst.owner_chat_id == '999'
        assert db.session.commit.called


def test_owner_link_rejects_wrong_or_missing_payload():
    inst = _Inst()
    with patch.object(tw, 'db'):
        assert tw._maybe_link_owner(inst, '/start link_WRONG', '999') is False
        assert tw._maybe_link_owner(inst, '/start', '999') is False
        assert inst.owner_chat_id is None


def test_extract_text_plain():
    inst = _Inst()
    assert tw._extract_text(inst, {'text': 'hallo'}) == ('hallo', False)


def test_extract_text_caption_fallback():
    inst = _Inst()
    assert tw._extract_text(inst, {'caption': 'foto text'}) == ('foto text', False)


def test_extract_text_voice_is_transcribed():
    inst = _Inst()
    with patch.object(tw, 'telegram_client') as tgc, \
         patch('app.services.stt.transcribe_audio_base64', return_value='transcript'):
        tgc.download_file.return_value = (b'oggbytes', 'audio/ogg')
        text, is_voice = tw._extract_text(inst, {'voice': {'file_id': 'F1'}})
        assert text == 'transcript'
        assert is_voice is True
