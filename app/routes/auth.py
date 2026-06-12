import os
import json
import hmac
import base64
import hashlib
import secrets
import logging
import stripe
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.exceptions import TooManyRequests
from app import db, limiter
from app.models import User, Subscription, TRIAL_DAYS

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

FB_API_VERSION = os.environ.get('FACEBOOK_API_VERSION', 'v23.0')


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


def _delete_user_data(user):
    """Full GDPR account deletion: Stripe sub, Evolution instances, DB rows.
    Reused by the dashboard delete button and the Meta data-deletion callback."""
    if user.subscription and user.subscription.stripe_subscription_id:
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
        try:
            stripe.Subscription.cancel(user.subscription.stripe_subscription_id)
        except Exception:
            pass

    from app.services.evolution import evolution_client
    from app.models import WhatsAppInstance
    for inst in WhatsAppInstance.query.filter_by(user_id=user.id).all():
        try:
            evolution_client.delete_instance(inst.instance_name, inst.api_token)
        except Exception:
            pass

    # Detach rows that would violate FK constraints on user delete:
    # affiliate sales stay for partner accounting (anonymized),
    # subscription rows go with the account.
    from app.models import AffiliateUsage
    AffiliateUsage.query.filter_by(user_id=user.id).update(
        {'user_id': None}, synchronize_session=False
    )
    Subscription.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.expire_all()

    db.session.delete(user)
    db.session.commit()


@auth_bp.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    user = current_user._get_current_object()
    logout_user()
    _delete_user_data(user)
    flash('Dein Konto wurde gelöscht.', 'info')
    return redirect(url_for('main.index'))


# ───────────────────────────────────────────────────────────────────────────────
# Login with Facebook (Meta OAuth, server-side flow — no JS SDK needed)
#
# Meta app setup (developers.facebook.com):
#   Product "Facebook Login" → Valid OAuth Redirect URIs:
#     https://whatsappbothelfer.de/auth/facebook/callback
#   Data Deletion Callback URL:
#     https://whatsappbothelfer.de/auth/facebook/data-deletion
# Env: FACEBOOK_APP_ID, FACEBOOK_APP_SECRET
# ───────────────────────────────────────────────────────────────────────────────

@auth_bp.route('/facebook')
def facebook_login():
    app_id = os.environ.get('FACEBOOK_APP_ID', '')
    if not app_id:
        flash('Facebook-Login ist derzeit nicht verfügbar.', 'error')
        return redirect(url_for('auth.login'))

    state = secrets.token_urlsafe(24)
    session['fb_oauth_state'] = state
    params = urlencode({
        'client_id': app_id,
        'redirect_uri': url_for('auth.facebook_callback', _external=True, _scheme='https'),
        'state': state,
        'scope': 'public_profile,email',
    })
    return redirect(f'https://www.facebook.com/{FB_API_VERSION}/dialog/oauth?{params}')


@auth_bp.route('/facebook/callback')
@limiter.limit('20 per minute')
def facebook_callback():
    if request.args.get('error'):
        flash('Facebook-Anmeldung abgebrochen.', 'info')
        return redirect(url_for('auth.login'))

    state = request.args.get('state', '')
    if not state or state != session.pop('fb_oauth_state', None):
        flash('Ungültige Anfrage. Bitte versuche es erneut.', 'error')
        return redirect(url_for('auth.login'))

    code = request.args.get('code', '')
    if not code:
        return redirect(url_for('auth.login'))

    try:
        # Exchange code for access token
        token_resp = requests.get(
            f'https://graph.facebook.com/{FB_API_VERSION}/oauth/access_token',
            params={
                'client_id': os.environ.get('FACEBOOK_APP_ID', ''),
                'client_secret': os.environ.get('FACEBOOK_APP_SECRET', ''),
                'redirect_uri': url_for('auth.facebook_callback', _external=True, _scheme='https'),
                'code': code,
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get('access_token', '')

        # Fetch profile
        me_resp = requests.get(
            f'https://graph.facebook.com/{FB_API_VERSION}/me',
            params={'fields': 'id,name,email', 'access_token': access_token},
            timeout=15,
        )
        me_resp.raise_for_status()
        profile = me_resp.json()
    except Exception as e:
        logger.error(f'Facebook OAuth failed: {e}')
        flash('Facebook-Anmeldung fehlgeschlagen. Bitte versuche es erneut.', 'error')
        return redirect(url_for('auth.login'))

    fb_id = str(profile.get('id', ''))
    fb_name = (profile.get('name') or '').strip()
    fb_email = (profile.get('email') or '').strip().lower()

    if not fb_id:
        flash('Facebook-Anmeldung fehlgeschlagen.', 'error')
        return redirect(url_for('auth.login'))

    # 1) Existing Facebook-linked account
    user = User.query.filter_by(facebook_id=fb_id).first()

    # 2) Link by e-mail if the user registered classically before
    if user is None and fb_email:
        user = User.query.filter_by(email=fb_email).first()
        if user:
            user.facebook_id = fb_id
            db.session.commit()

    # 3) New signup via Facebook
    if user is None:
        if not fb_email:
            # No email permission granted (e.g. phone-registered FB account)
            flash(
                'Facebook hat keine E-Mail-Adresse übermittelt. '
                'Bitte registriere dich mit deiner E-Mail.', 'error'
            )
            return redirect(url_for('auth.register'))

        user = User(
            name=fb_name or fb_email.split('@')[0],
            email=fb_email,
            facebook_id=fb_id,
            trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
        )
        user.set_password(secrets.token_urlsafe(32))   # random — login via Facebook
        db.session.add(user)
        db.session.flush()
        db.session.add(Subscription(user_id=user.id, status='inactive'))
        db.session.commit()
        flash(f'Willkommen, {user.name}! Du hast {TRIAL_DAYS} Tage kostenlos.', 'success')
    else:
        flash(f'Willkommen zurück, {user.name}!', 'success')

    login_user(user, remember=True)
    return redirect(url_for('dashboard.index'))


def _parse_signed_request(signed_request: str, secret: str):
    """Verify and decode Meta's signed_request (HMAC-SHA256, base64url)."""
    try:
        sig_b64, payload_b64 = signed_request.split('.', 1)

        def b64d(s: str) -> bytes:
            return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))

        expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(b64d(sig_b64), expected):
            return None
        return json.loads(b64d(payload_b64))
    except Exception:
        return None


@auth_bp.route('/facebook/data-deletion', methods=['POST'])
def facebook_data_deletion():
    """Meta Data Deletion Callback: user requested deletion via Facebook settings.
    Must respond with a status URL + confirmation code (Meta requirement)."""
    secret = os.environ.get('FACEBOOK_APP_SECRET', '')
    data = _parse_signed_request(request.form.get('signed_request', ''), secret) if secret else None
    if not data:
        return jsonify({'error': 'invalid signed_request'}), 400

    fb_id = str(data.get('user_id', ''))
    confirmation = f"del_{fb_id}_{int(datetime.utcnow().timestamp())}"

    user = User.query.filter_by(facebook_id=fb_id).first()
    if user:
        try:
            _delete_user_data(user)
            logger.info(f'Meta data-deletion: user fb={fb_id} deleted')
        except Exception as e:
            logger.error(f'Meta data-deletion failed for fb={fb_id}: {e}')
            return jsonify({'error': 'deletion failed'}), 500

    return jsonify({
        'url': url_for('auth.facebook_deletion_status', _external=True, _scheme='https', code=confirmation),
        'confirmation_code': confirmation,
    })


@auth_bp.route('/facebook/deletion-status')
def facebook_deletion_status():
    code = request.args.get('code', '')
    return (
        f"<html><body style='font-family:sans-serif;padding:40px;'>"
        f"<h2>Datenlöschung</h2>"
        f"<p>Deine mit Facebook verknüpften Daten wurden gelöscht.</p>"
        f"<p>Bestätigungscode: <code>{code[:80]}</code></p>"
        f"</body></html>"
    )
