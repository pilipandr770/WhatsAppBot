"""Customer-facing legal text block: personalization + route rendering."""

from types import SimpleNamespace as N

from app.services.legal_snippet import build_privacy_snippet


def test_snippet_personalizes_channel_provider_and_calendar():
    s = build_privacy_snippet(
        N(company='Muster GmbH', email='info@muster.de'),
        N(channel='telegram', google_token=None),
        N(ai_provider='anthropic', calendar_enabled=True),
    )
    assert 'Muster GmbH' in s
    assert 'Telegram' in s
    assert 'Art. 50' in s
    assert 'Anthropic' in s
    assert 'Termin' in s  # calendar section present


def test_snippet_uses_placeholders_when_company_missing():
    s = build_privacy_snippet(
        N(company=None, email=None),
        N(channel='whatsapp', google_token=None),
        N(ai_provider='mistral', calendar_enabled=False),
    )
    assert '[DEIN UNTERNEHMEN]' in s
    assert '[DEINE KONTAKT-E-MAIL]' in s
    assert 'WhatsApp' in s
    assert 'Mistral' in s
    assert 'Termin' not in s  # no calendar → no appointment line


def test_local_model_states_no_third_party_transfer():
    s = build_privacy_snippet(
        N(company='X', email='a@b.de'),
        N(channel='whatsapp', google_token=None),
        N(ai_provider='local', calendar_enabled=False),
    )
    assert 'selbstgehosteten' in s


def test_legal_snippet_route_renders(app, make_instance):
    inst = make_instance(channel='telegram', instance_name='tg_legal', api_token='123:ABC')
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(inst.user_id)
    # Log in the seeded user by creating one the login manager can load
    from app import db
    from app.models import User
    u = db.session.get(User, inst.user_id)
    if u is None:
        u = User(id=inst.user_id, email='owner@test.de', password_hash='x')
        db.session.add(u)
        db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(inst.user_id)
    resp = client.get(f'/dashboard/instance/{inst.id}/legal-snippet')
    assert resp.status_code == 200
    assert b'Datenschutz-Textbaustein' in resp.data
