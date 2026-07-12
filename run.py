from app import create_app, db
from app.models import User, Subscription, WhatsAppInstance, BotConfig, Document, DocumentChunk, Conversation, Message, GoogleToken, SiteConfig, AffiliateCode, AffiliateUsage, BotEvent, Product, ProductMedia, Appointment
from sqlalchemy import text
import os

app = create_app()

with app.app_context():
    # Serialize schema setup across concurrent gunicorn workers: without a lock,
    # two workers can both see a table as missing, both CREATE it, and the loser
    # crashes the whole boot (DuplicateTable → worker exit 3 → master shutdown).
    _ADVISORY_LOCK_ID = 872934017
    _is_pg = db.engine.dialect.name == 'postgresql'
    _lock_conn = db.engine.connect() if _is_pg else None
    if _lock_conn is not None:
        _lock_conn.execute(text(f"SELECT pg_advisory_lock({_ADVISORY_LOCK_ID})"))

    try:
        schema = os.environ.get('DB_SCHEMA', '')
        if schema:
            # Create schema if it doesn't exist, then set search_path
            with db.engine.connect() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                conn.execute(text(f'SET search_path TO "{schema}"'))
                conn.commit()

        try:
            db.create_all()
        except Exception as _e:
            # Belt-and-suspenders: a non-pg setup has no advisory lock, and a
            # concurrent worker may have created the tables already.
            print(f"db.create_all() skipped: {_e}")

        # ── Additive migrations (safe to run repeatedly) ──────────────────────
        # Add columns that were introduced after initial deployment.
        _additive_migrations = [
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS notification_phone VARCHAR(50)",
            # GDPR account deletion: affiliate sales are kept anonymized
            "ALTER TABLE affiliate_usages ALTER COLUMN user_id DROP NOT NULL",
            # Semantic RAG: embedding vector per chunk (JSON, NULL = keyword fallback)
            "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding TEXT",
            # Login with Facebook (Meta OAuth)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS facebook_id VARCHAR(100)",
            "CREATE INDEX IF NOT EXISTS ix_users_facebook_id ON users (facebook_id)",
            # Sign in with Google
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100)",
            "CREATE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id)",
            # Multi-provider LLM: user can choose Anthropic / Mistral / OpenAI / local
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS ai_provider VARCHAR(20) DEFAULT 'anthropic'",
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS ai_model VARCHAR(100)",
            # Built-in calendar
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS calendar_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS business_hours_start INTEGER DEFAULT 9",
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS business_hours_end INTEGER DEFAULT 18",
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS appointment_duration INTEGER DEFAULT 60",
            "ALTER TABLE bot_configs ADD COLUMN IF NOT EXISTS calendar_timezone VARCHAR(50) DEFAULT 'Europe/Berlin'",
        ]
        with db.engine.connect() as _conn:
            for _sql in _additive_migrations:
                try:
                    _conn.execute(text(_sql))
                except Exception:
                    pass  # column may already exist on fresh DBs created by create_all()
            _conn.commit()
    finally:
        if _lock_conn is not None:
            _lock_conn.execute(text(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_ID})"))
            _lock_conn.close()

# Background health monitor: syncs instance status with Evolution API every
# 5 min, auto-reconnects and alerts the admin (see app/services/health_monitor.py).
from app.services.health_monitor import start_health_monitor
start_health_monitor(app)

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
