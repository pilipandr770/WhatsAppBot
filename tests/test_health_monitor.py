"""Health monitor must only police WhatsApp/Evolution instances — never touch
Telegram instances (regression guard for the multi-channel work)."""

from unittest.mock import patch

from app.services import health_monitor as hm


def test_telegram_instance_not_flipped_or_alerted(app, make_instance):
    tg = make_instance(channel='telegram', status='connected', api_token='123:ABC',
                       instance_name='tg_1_live')
    wa = make_instance(channel='whatsapp', status='connected', api_token='T',
                       instance_name='wa_1_live')
    alerts = []
    with patch('app.services.evolution.evolution_client') as evo, \
         patch.object(hm, '_notify_admin', side_effect=lambda *a, **k: alerts.append(a)):
        evo.fetch_instance_names.return_value = set()      # Evolution knows nothing
        evo.get_connection_state.return_value = 'close'
        hm._tick(app)
        from app import db
        db.session.refresh(tg)
        db.session.refresh(wa)

    # Telegram instance is left completely alone
    assert tg.status == 'connected'
    # WhatsApp instance IS policed (missing from Evolution → disconnected)
    assert wa.status == 'disconnected'
    # No alert mentions the Telegram instance
    assert all('tg_1_live' not in str(a) for a in alerts)


def test_reverse_sync_ignores_telegram(app, make_instance):
    # A Telegram instance stuck at 'connecting' must not be probed against Evolution.
    tg = make_instance(channel='telegram', status='connecting', api_token='123:ABC',
                       instance_name='tg_1_conn')
    with patch('app.services.evolution.evolution_client') as evo, \
         patch.object(hm, '_notify_admin'):
        evo.fetch_instance_names.return_value = {'tg_1_conn'}  # even if name collided
        evo.get_connection_state.return_value = 'open'
        hm._tick(app)
        from app import db
        db.session.refresh(tg)
    # Not promoted to connected via the WhatsApp reverse-sync path
    assert tg.status == 'connecting'
