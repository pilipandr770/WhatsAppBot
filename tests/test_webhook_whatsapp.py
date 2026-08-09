"""WhatsApp end-to-end regression: an Evolution webhook message must still be
parsed, run through the AI pipeline, persisted, and answered via Evolution —
unchanged by the multi-channel refactor and the compliance layer.
"""

from unittest.mock import MagicMock, patch

from app.models import Conversation, Message


def _evolution_payload(text='Hallo', jid='4915000@s.whatsapp.net'):
    return {
        'event': 'messages.upsert',
        'data': {
            'key': {'remoteJid': jid, 'fromMe': False, 'id': 'M1'},
            'message': {'conversation': text},
            'pushName': 'Kunde',
        },
    }


def _mock_provider(capture):
    def fake(system_prompt, messages, rag_context, max_tokens, model):
        capture['system_prompt'] = system_prompt
        capture['messages'] = messages
        return 'Hallo, wie kann ich helfen?'
    prov = MagicMock()
    prov.get_ai_response.side_effect = fake
    return prov


def test_whatsapp_reply_dispatched_via_evolution(make_instance):
    inst = make_instance(channel='whatsapp', instance_name='wa_1_test')
    from app.routes import webhook as wh
    cap = {}
    with patch.object(wh, '_get_llm_provider', return_value=_mock_provider(cap)), \
         patch('app.services.evolution.evolution_client') as evo:
        wh._process_message('wa_1_test', _evolution_payload())
        assert evo.send_text.called
        kwargs = evo.send_text.call_args.kwargs
        assert kwargs['instance_name'] == 'wa_1_test'
        assert kwargs['to_jid'] == '4915000@s.whatsapp.net'
        assert kwargs['text'] == 'Hallo, wie kann ich helfen?'


def test_whatsapp_applies_compliance_prefix_and_keeps_owner_prompt(make_instance):
    make_instance(channel='whatsapp', instance_name='wa_1_test',
                  system_prompt='Du bist der Assistent von Muster GmbH.')
    from app.routes import webhook as wh
    cap = {}
    with patch.object(wh, '_get_llm_provider', return_value=_mock_provider(cap)), \
         patch('app.services.evolution.evolution_client'):
        wh._process_message('wa_1_test', _evolution_payload())
    sp = cap['system_prompt']
    assert sp.startswith('=== VERBINDLICHE PLATTFORM-REGELN')
    assert 'Muster GmbH' in sp
    assert 'nur für diese erste Antwort' in sp  # disclosure on the first message


def test_whatsapp_persists_conversation_and_messages(make_instance):
    inst = make_instance(channel='whatsapp', instance_name='wa_1_test')
    from app.routes import webhook as wh
    cap = {}
    with patch.object(wh, '_get_llm_provider', return_value=_mock_provider(cap)), \
         patch('app.services.evolution.evolution_client'):
        wh._process_message('wa_1_test', _evolution_payload())
    conv = Conversation.query.filter_by(instance_id=inst.id).first()
    assert conv is not None
    assert conv.message_count == 2
    roles = [m.role for m in Message.query.filter_by(conversation_id=conv.id).order_by(Message.id).all()]
    assert roles == ['user', 'assistant']


def test_disclosure_not_repeated_on_second_message(make_instance):
    make_instance(channel='whatsapp', instance_name='wa_1_test')
    from app.routes import webhook as wh
    cap = {}
    with patch.object(wh, '_get_llm_provider', return_value=_mock_provider(cap)), \
         patch('app.services.evolution.evolution_client'):
        wh._process_message('wa_1_test', _evolution_payload())   # new conversation
        wh._process_message('wa_1_test', _evolution_payload())   # returning
    # cap now holds the SECOND call's prompt
    assert 'nur für diese erste Antwort' not in cap['system_prompt']
    assert cap['system_prompt'].startswith('=== VERBINDLICHE PLATTFORM-REGELN')


def test_outgoing_and_group_messages_are_skipped(make_instance):
    make_instance(channel='whatsapp', instance_name='wa_1_test')
    from app.routes import webhook as wh
    with patch('app.services.evolution.evolution_client') as evo, \
         patch.object(wh, '_get_llm_provider') as prov:
        # fromMe → skip
        p = _evolution_payload()
        p['data']['key']['fromMe'] = True
        wh._process_message('wa_1_test', p)
        # group jid → skip
        wh._process_message('wa_1_test', _evolution_payload(jid='123@g.us'))
        assert not prov.called
        assert not evo.send_text.called


def test_inactive_config_does_not_answer(make_instance):
    inst = make_instance(channel='whatsapp', instance_name='wa_1_test')
    inst.bot_config.is_active = False
    from app import db
    db.session.commit()
    from app.routes import webhook as wh
    with patch('app.services.evolution.evolution_client') as evo, \
         patch.object(wh, '_get_llm_provider') as prov:
        wh._process_message('wa_1_test', _evolution_payload())
        assert not prov.called
        assert not evo.send_text.called
