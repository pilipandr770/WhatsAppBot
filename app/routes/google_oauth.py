"""
Google OAuth 2.0 routes for Calendar + Sheets integration.

Endpoints:
  GET  /oauth/google/authorize/<instance_id>  — start OAuth flow
  GET  /oauth/google/callback                  — handle Google redirect
  POST /oauth/google/disconnect/<instance_id>  — revoke & delete token

State strategy:
  We encode instance_id directly in the state string ("inst_<id>_<uid>") and
  verify ownership in the callback rather than relying on Flask sessions.
  This is resilient to session loss from login redirects or multi-worker deploys.
"""

import os
import json
import hmac
import hashlib
import logging
from datetime import datetime, timedelta

import requests as _requests
from flask import Blueprint, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app import db
from app.models import WhatsAppInstance, GoogleToken

google_oauth_bp = Blueprint('google_oauth', __name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets',
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
]

# REDIRECT_URI is resolved lazily so APP_BASE_URL is always read at runtime
def _redirect_uri():
    base = os.environ.get('APP_BASE_URL', 'https://whatsappbothelfer.de').rstrip('/')
    return f"{base}/oauth/google/callback"

GOOGLE_AUTH_URL   = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL  = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO   = 'https://www.googleapis.com/oauth2/v3/userinfo'
GOOGLE_REVOKE_URL = 'https://oauth2.googleapis.com/revoke'


def _client_cfg():
    return {
        'client_id':     os.environ.get('GOOGLE_CLIENT_ID', ''),
        'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
    }


# ---------------------------------------------------------------------------
# State helpers — encode/decode instance_id without session dependency
# ---------------------------------------------------------------------------

def _make_state(instance_id: int, user_id: int) -> str:
    """
    Build a signed state string: "inst_<instance_id>_<user_id>_<sig>"
    where sig = HMAC-SHA256(f"{instance_id}:{user_id}", SECRET_KEY)[:12].
    Allows verification at callback without storing anything in the session.
    """
    secret = os.environ.get('SECRET_KEY', 'dev-secret').encode()
    payload = f"{instance_id}:{user_id}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:12]
    return f"inst_{instance_id}_{user_id}_{sig}"


def _parse_state(state: str, expected_user_id: int):
    """
    Parse and verify the state string.
    Returns instance_id (int) on success, or None if invalid.
    """
    try:
        parts = state.split('_')
        # format: inst _ <instance_id> _ <user_id> _ <sig>
        if len(parts) != 4 or parts[0] != 'inst':
            return None
        instance_id = int(parts[1])
        user_id     = int(parts[2])
        sig         = parts[3]

        if user_id != expected_user_id:
            return None

        # Re-compute expected signature
        secret = os.environ.get('SECRET_KEY', 'dev-secret').encode()
        payload = f"{instance_id}:{user_id}"
        expected_sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:12]

        if not hmac.compare_digest(sig, expected_sig):
            return None

        return instance_id
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Authorize — start OAuth flow
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/authorize/<int:instance_id>')
@login_required
def authorize(instance_id):
    """Redirect the user to Google's OAuth consent screen."""
    # Verify ownership
    WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    cfg = _client_cfg()
    if not cfg['client_id']:
        flash('Google OAuth ist nicht konfiguriert (GOOGLE_CLIENT_ID fehlt).', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    state = _make_state(instance_id, current_user.id)

    from urllib.parse import urlencode
    params = {
        'client_id':     cfg['client_id'],
        'redirect_uri':  _redirect_uri(),
        'response_type': 'code',
        'scope':         ' '.join(SCOPES),
        'access_type':   'offline',   # request refresh token
        'prompt':        'consent',   # always show consent → ensures refresh_token
        'state':         state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    logger.info(f"[GoogleOAuth] instance={instance_id} user={current_user.id} → Google consent")
    return redirect(auth_url)


# ---------------------------------------------------------------------------
# Callback — exchange code for tokens
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/callback')
@login_required
def callback():
    """Handle the OAuth callback from Google."""
    # If the user denied access
    error = request.args.get('error')
    if error:
        flash(f'Google-Autorisierung abgebrochen: {error}', 'error')
        return redirect(url_for('dashboard.index'))

    code  = request.args.get('code', '')
    state = request.args.get('state', '')

    if not code or not state:
        flash('Ungültiger OAuth-Callback (fehlende Parameter). Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.index'))

    # Verify state — no session needed
    instance_id = _parse_state(state, current_user.id)
    if not instance_id:
        logger.warning(
            f"[GoogleOAuth] State mismatch user={current_user.id} state={state!r}"
        )
        flash('Ungültiger OAuth-State. Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.index'))

    # Verify instance ownership
    instance = WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    cfg = _client_cfg()

    # Exchange code for tokens
    try:
        token_resp = _requests.post(GOOGLE_TOKEN_URL, data={
            'code':          code,
            'client_id':     cfg['client_id'],
            'client_secret': cfg['client_secret'],
            'redirect_uri':  _redirect_uri(),
            'grant_type':    'authorization_code',
        }, timeout=15)
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as e:
        logger.error(f"[GoogleOAuth] Token exchange failed: {e}", exc_info=True)
        flash('Fehler beim Token-Austausch mit Google. Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    access_token  = token_data.get('access_token', '')
    refresh_token = token_data.get('refresh_token', '')
    expires_in    = token_data.get('expires_in', 3600)

    if not access_token:
        flash('Kein Access-Token von Google erhalten.', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    expiry = datetime.utcnow() + timedelta(seconds=int(expires_in) - 60)

    # Fetch user email
    google_email = ''
    try:
        info = _requests.get(
            GOOGLE_USERINFO,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )
        if info.ok:
            google_email = info.json().get('email', '')
    except Exception:
        pass

    # Upsert GoogleToken row
    existing = GoogleToken.query.filter_by(instance_id=instance_id).first()
    if existing:
        existing.access_token  = access_token
        if refresh_token:          # Google only returns it on first consent
            existing.refresh_token = refresh_token
        existing.token_expiry  = expiry
        existing.google_email  = google_email
        existing.scopes        = json.dumps(SCOPES)
        existing.updated_at    = datetime.utcnow()
    else:
        db.session.add(GoogleToken(
            instance_id=instance_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=expiry,
            google_email=google_email,
            scopes=json.dumps(SCOPES),
        ))

    db.session.commit()

    label = f' ({google_email})' if google_email else ''
    flash(f'✅ Google erfolgreich verbunden{label}!', 'success')
    logger.info(f"[GoogleOAuth] instance={instance_id} connected as {google_email!r}")
    return redirect(url_for('dashboard.bot_config', instance_id=instance_id))


# ---------------------------------------------------------------------------
# Disconnect — revoke & delete token
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/disconnect/<int:instance_id>', methods=['POST'])
@login_required
def disconnect(instance_id):
    """Revoke Google token and remove from DB."""
    WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    token_row = GoogleToken.query.filter_by(instance_id=instance_id).first()
    if token_row:
        try:
            _requests.post(
                GOOGLE_REVOKE_URL,
                params={'token': token_row.access_token},
                timeout=10
            )
        except Exception:
            pass
        db.session.delete(token_row)
        db.session.commit()
        flash('Google-Verbindung getrennt.', 'success')
    else:
        flash('Kein Google-Token gefunden.', 'info')

    return redirect(url_for('dashboard.bot_config', instance_id=instance_id))
