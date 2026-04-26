from app import db, login_manager
from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

TRIAL_DAYS = 3


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100))
    company = db.Column(db.String(150))
    stripe_customer_id = db.Column(db.String(100))
    is_admin = db.Column(db.Boolean, default=False)
    trial_ends_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    instances = db.relationship('WhatsAppInstance', backref='user', lazy=True, cascade='all, delete-orphan')
    subscription = db.relationship('Subscription', backref='user', uselist=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def active_instances_count(self):
        return WhatsAppInstance.query.filter_by(user_id=self.id).count()

    @property
    def is_in_trial(self):
        return bool(self.trial_ends_at and datetime.utcnow() < self.trial_ends_at)

    @property
    def trial_days_left(self):
        if self.trial_ends_at:
            return max(0, (self.trial_ends_at - datetime.utcnow()).days)
        return 0

    @property
    def has_access(self):
        """True if user can use the bot (active subscription OR in trial)."""
        if self.subscription and self.subscription.is_active:
            return True
        return self.is_in_trial

    @property
    def instances_limit(self):
        if self.subscription and self.subscription.is_active:
            return self.subscription.instances_limit
        if self.is_in_trial:
            return 1
        return 0

    @property
    def can_add_instance(self):
        return self.active_instances_count < self.instances_limit


class Subscription(db.Model):
    __tablename__ = 'subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    stripe_subscription_id = db.Column(db.String(100))
    stripe_price_id = db.Column(db.String(100))
    status = db.Column(db.String(50), default='inactive')  # active, inactive, canceled, trialing
    plan = db.Column(db.String(50), default='starter')     # starter, pro, business
    instances_limit = db.Column(db.Integer, default=1)
    current_period_end = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_active(self):
        return self.status in ('active', 'trialing')


class WhatsAppInstance(db.Model):
    __tablename__ = 'whatsapp_instances'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    instance_name = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(100))
    phone_number = db.Column(db.String(50))
    status = db.Column(db.String(50), default='disconnected')  # disconnected, connecting, connected
    api_token = db.Column(db.String(255))
    qr_code = db.Column(db.Text)
    qr_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bot_config = db.relationship('BotConfig', backref='instance', uselist=False, cascade='all, delete-orphan')
    documents = db.relationship('Document', backref='instance', lazy=True, cascade='all, delete-orphan')
    conversations = db.relationship('Conversation', backref='instance', lazy=True, cascade='all, delete-orphan')
    google_token = db.relationship('GoogleToken', backref='instance', uselist=False, cascade='all, delete-orphan')

    @property
    def is_connected(self):
        return self.status == 'connected'

    @property
    def total_messages(self):
        count = 0
        for conv in self.conversations:
            count += conv.message_count or 0
        return count


class BotConfig(db.Model):
    __tablename__ = 'bot_configs'
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('whatsapp_instances.id'), nullable=False)
    bot_name = db.Column(db.String(100), default='KI-Assistent')
    system_prompt = db.Column(db.Text, default=(
        'Du bist ein freundlicher und professioneller Kundenservice-Assistent. '
        'Beantworte alle Fragen höflich, präzise und auf Deutsch. '
        'Wenn du etwas nicht weißt, sage es ehrlich.'
    ))
    language = db.Column(db.String(20), default='de')
    max_tokens = db.Column(db.Integer, default=500)
    use_rag = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    # Cooldown: avoid answering too fast (seconds)
    response_delay = db.Column(db.Integer, default=2)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Document(db.Model):
    __tablename__ = 'documents'
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('whatsapp_instances.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255))
    file_type = db.Column(db.String(50))  # pdf, docx, txt
    file_size = db.Column(db.Integer)
    status = db.Column(db.String(50), default='processing')  # processing, ready, error
    chunk_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chunks = db.relationship('DocumentChunk', backref='document', lazy=True, cascade='all, delete-orphan')


class DocumentChunk(db.Model):
    __tablename__ = 'document_chunks'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=False)
    instance_id = db.Column(db.Integer, db.ForeignKey('whatsapp_instances.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    chunk_index = db.Column(db.Integer, default=0)


class Conversation(db.Model):
    __tablename__ = 'conversations'
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('whatsapp_instances.id'), nullable=False)
    contact_jid = db.Column(db.String(100), nullable=False)
    contact_name = db.Column(db.String(100))
    message_count = db.Column(db.Integer, default=0)
    last_message_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship('Message', backref='conversation', lazy=True, cascade='all, delete-orphan', order_by='Message.created_at')

    __table_args__ = (db.UniqueConstraint('instance_id', 'contact_jid', name='uq_instance_contact'),)

    @property
    def phone_number(self):
        return self.contact_jid.split('@')[0]


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # user, assistant
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GoogleToken(db.Model):
    """Stores Google OAuth 2.0 tokens per WhatsApp instance."""
    __tablename__ = 'google_tokens'
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey('whatsapp_instances.id'), nullable=False, unique=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text)
    token_expiry = db.Column(db.DateTime)   # UTC expiry of the access token
    google_email = db.Column(db.String(255))
    scopes = db.Column(db.Text)             # JSON-encoded list of granted scopes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
