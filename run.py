from app import create_app, db
from app.models import User, Subscription, WhatsAppInstance, BotConfig, Document, DocumentChunk, Conversation, Message
import os

app = create_app()

# Only run schema creation when explicitly requested via environment variable.
# On hosted platforms (Render), the database may be provided separately and
# the app should not block startup waiting for DB during worker boot.
if os.environ.get('RUN_MIGRATIONS', '').lower() in ('1', 'true', 'yes'):
    with app.app_context():
        db.create_all()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
