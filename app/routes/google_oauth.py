"""
Google OAuth 2.0 routes for Calendar + Sheets integration.

Endpoints:
  GET  /oauth/google/authorize/<instance_id>  — start OAuth flow
  GET  /oauth/google/callback                  — handle Google redirect
  POST /oauth/google/disconnect/<instance_id>  — revoke & delete token
"""

import os
import json
import logging
from datetime import timezone

import requests as _requests
from flask import Blueprint, redirect, url_for, flash, request, session
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

_APP_BASE = os.environ.get('APP_BASE_URL', 'https://whatsappbothelfer.de').rstrip('/')
REDIRECT_URI = f"{_APP_BASE}/oauth/google/callback"

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
GOOGLE_REVOKE_URL = 'https://oauth2.googleapis.com/revoke'


def _client_config():
    return {
        'client_id': os.environ.get('GOOGLE_CLIENT_ID', ''),
        'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
    }


# ---------------------------------------------------------------------------
# Authorize — start OAuth flow
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/authorize/<int:instance_id>')
@login_required
def authorize(instance_id):
    """Redirect the user to Google's OAuth consent screen."""
    # Verify ownership
    instance = WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    cfg = _client_config()
    if not cfg['client_id']:
        flash('Google OAuth ist nicht konfiguriert (GOOGLE_CLIENT_ID fehlt).', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    # Store instance_id in session so callback can retrieve it
    state = f"inst_{instance_id}"
    session['google_oauth_state'] = state
    session['google_oauth_instance_id'] = instance_id

    from urllib.parse import urlencode
    params = {
        'client_id': cfg['client_id'],
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': ' '.join(SCOPES),
        'access_type': 'offline',    # request refresh token
        'prompt': 'consent',         # always show consent → ensures refresh_token
        'state': state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    logger.info(f"[GoogleOAuth] Redirecting instance {instance_id} to Google consent screen")
    return redirect(auth_url)


# ---------------------------------------------------------------------------
# Callback — exchange code for tokens
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/callback')
@login_required
def callback():
    """Handle the OAuth callback from Google."""
    error = request.args.get('error')
    if error:
        flash(f'Google-Autorisierung abgebrochen: {error}', 'error')
        return redirect(url_for('dashboard.index'))

    code = request.args.get('code', '')
    state = request.args.get('state', '')

    # Verify state
    expected_state = session.pop('google_oauth_state', None)
    instance_id = session.pop('google_oauth_instance_id', None)

    if not expected_state or state != expected_state or not instance_id:
        flash('Ungültiger OAuth-State. Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.index'))

    # Verify ownership again (session could theoretically be hijacked)
    instance = WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    cfg = _client_config()

    # Exchange code for tokens
    try:
        token_resp = _requests.post(GOOGLE_TOKEN_URL, data={
            'code': code,
            'client_id': cfg['client_id'],
            'client_secret': cfg['client_secret'],
            'redirect_uri': REDIRECT_URI,
            'grant_type': 'authorization_code',
        }, timeout=15)
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as e:
        logger.error(f"[GoogleOAuth] Token exchange failed: {e}", exc_info=True)
        flash('Fehler beim Token-Austausch mit Google. Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    access_token = token_data.get('access_token', '')
    refresh_token = token_data.get('refresh_token', '')
    expires_in = token_data.get('expires_in', 3600)

    if not access_token:
        flash('Kein Access-Token von Google erhalten.', 'error')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    # Calculate expiry (UTC naive datetime)
    from datetime import datetime, timedelta
    expiry = datetime.utcnow() + timedelta(seconds=int(expires_in) - 60)

    # Fetch user email
    google_email = ''
    try:
        info_resp = _requests.get(
            GOOGLE_USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )
        if info_resp.ok:
            google_email = info_resp.json().get('email', '')
    except Exception:
        pass

    # Upsert GoogleToken row
    existing = GoogleToken.query.filter_by(instance_id=instance_id).first()
    if existing:
        existing.access_token = access_token
        if refresh_token:          # Google only returns refresh_token on first consent
            existing.refresh_token = refresh_token
        existing.token_expiry = expiry
        existing.google_email = google_email
        existing.scopes = json.dumps(SCOPES)
        from datetime import datetime as _dt
        existing.updated_at = _dt.utcnow()
    else:
        new_token = GoogleToken(
            instance_id=instance_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=expiry,
            google_email=google_email,
            scopes=json.dumps(SCOPES),
        )
        db.session.add(new_token)

    db.session.commit()

    email_display = f' ({google_email})' if google_email else ''
    flash(f'✅ Google erfolgreich verbunden{email_display}!', 'success')
    logger.info(f"[GoogleOAuth] instance {instance_id} connected as {google_email}")
    return redirect(url_for('dashboard.bot_config', instance_id=instance_id))


# ---------------------------------------------------------------------------
# Disconnect — revoke & delete token
# ---------------------------------------------------------------------------

@google_oauth_bp.route('/disconnect/<int:instance_id>', methods=['POST'])
@login_required
def disconnect(instance_id):
    """Revoke Google token and remove from DB."""
    instance = WhatsAppInstance.query.filter_by(
        id=instance_id, user_id=current_user.id
    ).first_or_404()

    token_row = GoogleToken.query.filter_by(instance_id=instance_id).first()
    if token_row:
        # Best-effort revoke at Google (don't fail if it errors)
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
