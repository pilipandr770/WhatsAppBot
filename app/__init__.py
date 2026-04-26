import logging
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import event, text

# Configure root logger so all module loggers are visible in Render logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s'
)

logger = logging.getLogger(__name__)

db           = SQLAlchemy()
login_manager = LoginManager()
migrate      = Migrate()
csrf         = CSRFProtect()
limiter      = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=os.environ.get('REDIS_URL', 'memory://'),  # Redis in prod, memory in dev
)


def _build_db_url():
    """Return DATABASE_URL, appending search_path option if DB_SCHEMA is set."""
    url = os.environ.get('DATABASE_URL', 'postgresql://wauser:changeme@localhost/whatsapp_saas')
    # Render uses postgres:// but SQLAlchemy needs postgresql://
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    schema = os.environ.get('DB_SCHEMA', '')
    if schema and 'search_path' not in url and 'options=' not in url:
        sep = '&' if '?' in url else '?'
        url = f"{url}{sep}options=-csearch_path%3D{schema}"
    return url


def create_app():
    app = Flask(__name__)

    # ── Security: warn loudly if SECRET_KEY is the insecure default ──────────
    secret_key = os.environ.get('SECRET_KEY', '')
    if not secret_key or secret_key == 'dev-secret':
        logger.warning(
            "⚠️  SECRET_KEY is not set or uses the insecure default 'dev-secret'. "
            "Set a strong random SECRET_KEY environment variable in production!"
        )
        secret_key = secret_key or 'dev-secret'
    app.config['SECRET_KEY'] = secret_key

    # ── Core config ───────────────────────────────────────────────────────────
    app.config['SQLALCHEMY_DATABASE_URI'] = _build_db_url()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB upload limit
    app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/app/uploads')

    # ── Session / cookie hardening ────────────────────────────────────────────
    app.config['SESSION_COOKIE_SECURE']   = not app.debug   # HTTPS only in prod
    app.config['SESSION_COOKIE_HTTPONLY'] = True             # no JS access
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'           # CSRF mitigation
    app.config['REMEMBER_COOKIE_SECURE']  = not app.debug
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

    # ── WTF / CSRF ────────────────────────────────────────────────────────────
    app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # token valid 1 h

    # ── Init extensions ───────────────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Bitte melde dich an.'
    login_manager.login_message_category = 'info'

    # ── Security response headers ─────────────────────────────────────────────
    @app.after_request
    def _set_security_headers(response):
        # Prevent clickjacking
        response.headers.setdefault('X-Frame-Options', 'DENY')
        # Prevent MIME-type sniffing
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # Minimal referrer leak
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Force HTTPS (only in production, 1 year)
        if not app.debug:
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains'
            )
        # Content Security Policy — restricts script/style origins
        # 'unsafe-inline' required for our inline styles/scripts; tighten later with nonces
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        return response

    # ── Register blueprints ───────────────────────────────────────────────────
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.webhook import webhook_bp
    from app.routes.billing import billing_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(webhook_bp, url_prefix='/wh')
    app.register_blueprint(billing_bp, url_prefix='/billing')
    app.register_blueprint(admin_bp, url_prefix='/admin')

    from app.routes.main import main_bp
    app.register_blueprint(main_bp)

    from app.routes.google_oauth import google_oauth_bp
    app.register_blueprint(google_oauth_bp, url_prefix='/oauth/google')

    # ── CSRF exemptions: JSON endpoints that don't use browser sessions ───────
    # Evolution API webhooks send raw JSON — exempt the whole blueprint.
    csrf.exempt(webhook_bp)
    # Stripe webhook is one specific route; exempt only that view function,
    # NOT the whole billing_bp (cancel/portal are browser forms that need CSRF).
    from app.routes.billing import stripe_webhook
    csrf.exempt(stripe_webhook)

    return app
