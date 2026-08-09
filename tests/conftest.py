"""Shared pytest fixtures.

Runs the app against an in-memory SQLite DB so tests never touch Postgres or
any live service. Every outbound integration (Evolution, Telegram, the LLM
providers) is mocked in the individual tests.
"""

import os

# Must be set BEFORE app is imported/created — create_app reads these at call time.
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('APP_BASE_URL', 'https://test.local')
os.environ.setdefault('CALENDAR_TIMEZONE', 'Europe/Berlin')
os.environ.setdefault('HEALTH_MONITOR_ENABLED', 'false')  # don't start the bg thread
os.environ.pop('DB_SCHEMA', None)

import pytest

from app import create_app, db as _db
from app.models import WhatsAppInstance, BotConfig


@pytest.fixture
def app():
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def make_instance(app):
    """Factory: create an instance (+ active BotConfig) of a given channel."""
    def _make(channel='whatsapp', status='connected', system_prompt='Du bist der Assistent von Muster GmbH.',
              api_token='TKN', instance_name=None, owner_chat_id=None,
              notification_phone=None, calendar_enabled=False, **cfg_kwargs):
        instance_name = instance_name or f"{'tg' if channel=='telegram' else 'wa'}_1_{status}_{api_token[:4]}"
        inst = WhatsAppInstance(
            user_id=1, instance_name=instance_name, display_name='Test',
            channel=channel, api_token=api_token, status=status,
            owner_chat_id=owner_chat_id,
        )
        _db.session.add(inst)
        _db.session.flush()
        cfg = BotConfig(
            instance_id=inst.id, is_active=True, system_prompt=system_prompt,
            max_tokens=500, notification_phone=notification_phone,
            calendar_enabled=calendar_enabled, **cfg_kwargs,
        )
        _db.session.add(cfg)
        _db.session.commit()
        return inst
    return _make
