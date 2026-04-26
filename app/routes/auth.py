from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.exceptions import TooManyRequests
from app import db, limiter
from app.models import User, Subscription, TRIAL_DAYS

auth_bp = Blueprint('auth', __name__)


@auth_bp.errorhandler(429)
def ratelimit_handler(e):
    flash('Zu viele Versuche. Bitte warte kurz und versuche es erneut.', 'error')
    return redirect(url_for('auth.login')), 429


def _safe_next(next_url: str) -> str:
    """Return next_url only if it is a relative path on the same host.
    Prevents open redirect: /auth/login?next=https://evil.com
    """
    if not next_url:
        return ''
    parsed = urlparse(next_url)
    # Allow only relative URLs (no scheme, no netloc)
    if parsed.scheme or parsed.netloc:
        return ''
    return next_url


@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit('10 per hour')   # max 10 registrations per IP per hour
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        company = request.form.get('company', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        consent = request.form.get('consent')
        if not consent:
            flash('Bitte akzeptiere die AGB und Datenschutzerklärung, um fortzufahren.', 'error')
            return render_template('auth/register.html')

        if not all([name, email, password]):
            flash('Bitte alle Pflichtfelder ausfüllen.', 'error')
            return render_template('auth/register.html')

        if password != password2:
            flash('Passwörter stimmen nicht überein.', 'error')
            return render_template('auth/register.html')

        if len(password) < 8:
            flash('Passwort muss mindestens 8 Zeichen haben.', 'error')
            return render_template('auth/register.html')

        if User.query.filter_by(email=email).first():
            flash('Diese E-Mail ist bereits registriert.', 'error')
            return render_template('auth/register.html')

        user = User(
            name=name, email=email, company=company,
            trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS)
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        sub = Subscription(user_id=user.id, status='inactive')
        db.session.add(sub)
        db.session.commit()

        login_user(user)
        flash(f'Willkommen, {name}! Du hast {TRIAL_DAYS} Tage kostenlos.', 'success')
        return redirect(url_for('dashboard.index'))

    return render_template('auth/register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('20 per minute;100 per hour')  # brute-force protection
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            # Fix open redirect: only allow relative same-site URLs
            next_page = _safe_next(request.args.get('next', ''))
            return redirect(next_page or url_for('dashboard.index'))
        else:
            # Generic message — don't reveal whether email exists
            flash('Ungültige E-Mail oder Passwort.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))


@auth_bp.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    user = current_user._get_current_object()

    # Cancel Stripe subscription if active
    if user.subscription and user.subscription.stripe_subscription_id:
        import stripe, os
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
        try:
            stripe.Subscription.cancel(user.subscription.stripe_subscription_id)
        except Exception:
            pass

    # Delete Evolution API instances
    from app.services.evolution import evolution_client
    from app.models import WhatsAppInstance
    for inst in WhatsAppInstance.query.filter_by(user_id=user.id).all():
        try:
            evolution_client.delete_instance(inst.instance_name, inst.api_token)
        except Exception:
            pass

    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash('Dein Konto wurde gelöscht.', 'info')
    return redirect(url_for('main.index'))
