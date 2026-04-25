from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from sqlalchemy import event, text
import os

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()


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

    # Config
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
    app.config['SQLALCHEMY_DATABASE_URI'] = _build_db_url()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB
    app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/app/uploads')

    # Init extensions
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Bitte melde dich an.'
    login_manager.login_message_category = 'info'

    # Register blueprints
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

    return app
