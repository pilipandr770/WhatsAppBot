from app import create_app, db
from app.models import User, Subscription, WhatsAppInstance, BotConfig, Document, DocumentChunk, Conversation, Message, GoogleToken, SiteConfig
from sqlalchemy import text
import os

app = create_app()

with app.app_context():
    schema = os.environ.get('DB_SCHEMA', '')
    if schema:
        # Create schema if it doesn't exist, then set search_path
        with db.engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}"'))
            conn.commit()
    db.create_all()

    # ── Additive migrations (safe to run repeatedly) ──────────────────────────
    # Add columns that were introduced after initial deployment.
    _additive_migrations = [
        "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS notification_phone VARCHAR(50)",
    ]
    with db.engine.connect() as _conn:
        for _sql in _additive_migrations:
            try:
                _conn.execute(text(_sql))
            except Exception as _e:
                pass  # column may already exist on fresh DBs created by create_all()
        _conn.commit()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
