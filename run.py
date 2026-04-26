from app import create_app, db
from app.models import User, Subscription, WhatsAppInstance, BotConfig, Document, DocumentChunk, Conversation, Message, GoogleToken
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

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
