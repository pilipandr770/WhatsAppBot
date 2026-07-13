import os
import uuid
import time
import threading
import logging
from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app import db
from app.models import WhatsAppInstance, BotConfig, Document, Conversation, Message, Subscription, Product, ProductMedia, Appointment
from app.services.evolution import evolution_client
from app.tasks import process_document

dashboard_bp = Blueprint('dashboard', __name__)
logger = logging.getLogger(__name__)

_VALID_TIMEZONES = {
    'Europe/Berlin', 'Europe/Vienna', 'Europe/Zurich', 'Europe/London',
    'Europe/Paris', 'Europe/Rome', 'Europe/Madrid', 'Europe/Warsaw',
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'Asia/Dubai', 'Asia/Istanbul', 'Asia/Almaty',
    'UTC',
}

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt', 'jpg', 'jpeg', 'png'}

# Magic bytes: first bytes that identify a file type regardless of extension
_MAGIC_BYTES = {
    'pdf':  b'%PDF',
    'docx': b'PK\x03\x04',   # ZIP-based (Office Open XML)
    'txt':  None,              # plain text has no magic bytes — skip check
    'jpg':  b'\xff\xd8\xff',
    'jpeg': b'\xff\xd8\xff',
    'png':  b'\x89PNG',
}

# Extensions that feed the RAG knowledge base (images are send-only media)
TEXT_EXTENSIONS = {'pdf', 'docx', 'txt'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_file_content(file_storage, ext: str) -> bool:
    """Check magic bytes to verify file content matches the declared extension."""
    magic = _MAGIC_BYTES.get(ext)
    if magic is None:
        return True   # txt — no check
    header = file_storage.read(len(magic))
    file_storage.seek(0)   # rewind for subsequent read/save
    return header == magic


# ─── Main dashboard ──────────────────────────────────────────────────────────

@dashboard_bp.route('/')
@login_required
def index():
    instances = WhatsAppInstance.query.filter_by(user_id=current_user.id).all()
    week_stats = _compute_week_stats([i.id for i in instances])
    return render_template('dashboard/index.html', instances=instances, week_stats=week_stats)


def _compute_week_stats(instance_ids):
    """ROI numbers for the last 7 days vs the 7 days before."""
    from datetime import timedelta
    from sqlalchemy import func
    from app.models import BotEvent

    empty = {
        'new_contacts': 0, 'new_contacts_prev': 0,
        'messages': 0, 'messages_prev': 0,
        'appointments': 0, 'appointments_prev': 0,
        'handoffs': 0, 'handoffs_prev': 0,
        'media_sent': 0,
    }
    if not instance_ids:
        return empty

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    def _count_contacts(start, end):
        return (
            db.session.query(func.count(Conversation.id))
            .filter(Conversation.instance_id.in_(instance_ids))
            .filter(Conversation.created_at >= start, Conversation.created_at < end)
            .scalar() or 0
        )

    def _count_messages(start, end):
        return (
            db.session.query(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .filter(Conversation.instance_id.in_(instance_ids))
            .filter(Message.created_at >= start, Message.created_at < end)
            .filter(Message.role == 'user')
            .scalar() or 0
        )

    def _count_events(event_type, start, end):
        return (
            db.session.query(func.count(BotEvent.id))
            .filter(BotEvent.instance_id.in_(instance_ids))
            .filter(BotEvent.event_type == event_type)
            .filter(BotEvent.created_at >= start, BotEvent.created_at < end)
            .scalar() or 0
        )

    return {
        'new_contacts':       _count_contacts(week_ago, now),
        'new_contacts_prev':  _count_contacts(two_weeks_ago, week_ago),
        'messages':           _count_messages(week_ago, now),
        'messages_prev':      _count_messages(two_weeks_ago, week_ago),
        'appointments':       _count_events('appointment', week_ago, now),
        'appointments_prev':  _count_events('appointment', two_weeks_ago, week_ago),
        'handoffs':           _count_events('handoff', week_ago, now),
        'handoffs_prev':      _count_events('handoff', two_weeks_ago, week_ago),
        'media_sent':         _count_events('media_sent', week_ago, now),
    }


# ─── Instances ───────────────────────────────────────────────────────────────

@dashboard_bp.route('/instance/create', methods=['POST'])
@login_required
def create_instance():
    if not current_user.can_add_instance:
        flash('Instanz-Limit erreicht. Bitte upgrade deinen Plan.', 'error')
        return redirect(url_for('billing.plans'))

    display_name = request.form.get('display_name', '').strip()
    if not display_name:
        flash('Bitte gib einen Namen ein.', 'error')
        return redirect(url_for('dashboard.index'))

    # Generate unique instance name
    instance_name = f"wa_{current_user.id}_{uuid.uuid4().hex[:8]}"

    try:
        _, token = evolution_client.create_instance(instance_name)

        instance = WhatsAppInstance(
            user_id=current_user.id,
            instance_name=instance_name,
            display_name=display_name,
            api_token=token,
            status='connecting'
        )
        db.session.add(instance)
        db.session.flush()

        # Default bot config
        config = BotConfig(instance_id=instance.id)
        db.session.add(config)
        db.session.commit()

        flash(f'Instanz "{display_name}" erstellt. Scanne jetzt den QR-Code.', 'success')
        return redirect(url_for('dashboard.connect_instance', instance_id=instance.id))

    except Exception as e:
        logger.error(f"Create instance error: {e}")
        flash('Fehler beim Erstellen der Instanz. Bitte versuche es erneut.', 'error')
        return redirect(url_for('dashboard.index'))


@dashboard_bp.route('/instance/<int:instance_id>/connect')
@login_required
def connect_instance(instance_id):
    instance = _get_instance(instance_id)
    if instance.channel == 'telegram':
        return redirect(url_for('dashboard.connect_telegram', instance_id=instance.id))
    return render_template('dashboard/connect.html', instance=instance)


# ─── Telegram channel ─────────────────────────────────────────────────────────

@dashboard_bp.route('/instance/create-telegram', methods=['POST'])
@login_required
def create_telegram_instance():
    """Create a Telegram-channel instance (bot token entered on the next screen)."""
    if not current_user.can_add_instance:
        flash('Kanal-Limit erreicht. Bitte upgrade deinen Plan.', 'error')
        return redirect(url_for('billing.plans'))

    display_name = request.form.get('display_name', '').strip()
    if not display_name:
        flash('Bitte gib einen Namen ein.', 'error')
        return redirect(url_for('dashboard.index'))

    instance_name = f"tg_{current_user.id}_{uuid.uuid4().hex[:8]}"
    instance = WhatsAppInstance(
        user_id=current_user.id,
        instance_name=instance_name,
        display_name=display_name,
        channel='telegram',
        status='disconnected',
    )
    db.session.add(instance)
    db.session.flush()
    db.session.add(BotConfig(instance_id=instance.id))
    db.session.commit()

    return redirect(url_for('dashboard.connect_telegram', instance_id=instance.id))


@dashboard_bp.route('/instance/<int:instance_id>/telegram')
@login_required
def connect_telegram(instance_id):
    instance = _get_instance(instance_id)
    if instance.channel != 'telegram':
        return redirect(url_for('dashboard.connect_instance', instance_id=instance.id))

    owner_link = None
    if instance.status == 'connected' and instance.telegram_username and instance.telegram_webhook_secret:
        owner_link = (
            f"https://t.me/{instance.telegram_username}"
            f"?start=link_{instance.telegram_webhook_secret}"
        )
    return render_template('dashboard/connect_telegram.html',
                           instance=instance, owner_link=owner_link)


@dashboard_bp.route('/instance/<int:instance_id>/telegram/save', methods=['POST'])
@login_required
def save_telegram_token(instance_id):
    """Validate the bot token via getMe and register the webhook."""
    import secrets as _secrets
    from app.services.telegram import telegram_client

    instance = _get_instance(instance_id)
    if instance.channel != 'telegram':
        return jsonify({'status': 'error', 'message': 'Falscher Kanal-Typ.'}), 400

    token = (request.form.get('bot_token') or '').strip()
    if not token or ':' not in token:
        flash('Bitte gib einen gültigen Bot-Token ein (Format: 123456:ABC…).', 'error')
        return redirect(url_for('dashboard.connect_telegram', instance_id=instance.id))

    bot_info = telegram_client.get_me(token)
    if not bot_info:
        flash('Token ungültig. Prüfe den Token von @BotFather und versuche es erneut.', 'error')
        return redirect(url_for('dashboard.connect_telegram', instance_id=instance.id))

    secret = _secrets.token_urlsafe(24)
    instance.api_token = token
    instance.telegram_username = bot_info.get('username')
    instance.telegram_webhook_secret = secret

    if telegram_client.set_webhook(token, instance.instance_name, secret):
        instance.status = 'connected'
        db.session.commit()
        flash(f'Telegram-Bot @{instance.telegram_username} erfolgreich verbunden!', 'success')
    else:
        instance.status = 'disconnected'
        db.session.commit()
        flash('Webhook konnte nicht registriert werden. Bitte später erneut versuchen.', 'error')

    return redirect(url_for('dashboard.connect_telegram', instance_id=instance.id))


@dashboard_bp.route('/instance/<int:instance_id>/qr')
@login_required
def get_qr(instance_id):
    instance = _get_instance(instance_id)

    # Already connected — no QR needed
    if instance.status == 'connected':
        return jsonify({'qr': '', 'status': 'connected'})

    # Return QR stored from webhook if still fresh (< 55 seconds old)
    if instance.qr_code and instance.qr_updated_at:
        age = (datetime.utcnow() - instance.qr_updated_at).total_seconds()
        if age < 55:
            return jsonify({'qr': instance.qr_code, 'status': instance.status})
        else:
            # QR expired — clear it
            instance.qr_code = None
            db.session.commit()

    # No fresh QR — call trigger_connect.
    # Some Evolution versions return the QR directly in the response;
    # others send it via QRCODE_UPDATED webhook asynchronously.
    # We handle both: save directly when present, otherwise the webhook handler will save it.
    _inst_name = instance.instance_name
    _inst_token = instance.api_token

    try:
        qr_direct = evolution_client.trigger_connect(_inst_name, _inst_token)
    except Exception as ex:
        logger.debug(f"[QR] trigger_connect {_inst_name}: {ex}")
        qr_direct = ''

    if qr_direct:
        # Evolution returned QR inline — save it immediately
        instance.qr_code = qr_direct
        instance.qr_updated_at = datetime.utcnow()
        db.session.commit()
        logger.info(f"[QR] stored inline QR for {_inst_name}")
        return jsonify({'qr': qr_direct, 'status': instance.status})

    # QR will arrive via webhook — frontend keeps polling
    return jsonify({'qr': '', 'status': instance.status})


@dashboard_bp.route('/instance/<int:instance_id>/reconnect', methods=['POST'])
@login_required
def reconnect_instance(instance_id):
    """Force-delete and re-create the Evolution API instance to get a fresh QR."""
    instance = _get_instance(instance_id)
    if instance.status == 'connected':
        return jsonify({'status': 'ok', 'message': 'Already connected'})

    # Guard: prevent a second concurrent reconnect within 15 seconds
    if instance.qr_updated_at:
        age = (datetime.utcnow() - instance.qr_updated_at).total_seconds()
        if age < 15:
            logger.info(f"[Reconnect] Cooldown active for {instance.instance_name} ({age:.0f}s ago) — skipping")
            return jsonify({'status': 'ok', 'message': 'Reconnect in progress, wait for QR...'})

    try:
        # Stamp pending marker BEFORE recreate so no parallel request can also trigger it
        instance.qr_updated_at = datetime.utcnow()
        instance.qr_code = None
        db.session.commit()
        _recreate_evolution_instance(instance)
        return jsonify({'status': 'ok', 'message': 'Reconnecting — QR incoming...'})
    except Exception as e:
        logger.error(f"reconnect_instance {instance_id}: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ─── Evolution helpers ────────────────────────────────────────────────────────

def _recreate_evolution_instance(instance: WhatsAppInstance):
    """Delete + re-create the Evolution API instance and save QR immediately.

    Evolution v2.3.x returns the QR inside the create response (qrcode.base64).
    We save it right away so the frontend doesn't have to wait for a webhook.
    The QRCODE_UPDATED webhook (if it arrives) will just overwrite with a fresh QR.

    Ordering:
    1. Delete (global key, ignores errors — instance may already be gone).
    2. sleep(3) for Evolution to finish cleanup.
    3. Create — retry once with another delete if 403/409 (instance still exists).
    4. Save new token + QR from create response immediately.
    """
    import requests as _req

    name = instance.instance_name
    logger.info(f"[Recreate] Deleting Evolution instance {name}")
    try:
        evolution_client.delete_instance(name, None)  # global_key fallback
    except Exception as e:
        logger.warning(f"[Recreate] delete_instance {name}: {e}")

    time.sleep(3)

    instance.status = 'connecting'
    db.session.commit()

    logger.info(f"[Recreate] Creating Evolution instance {name}")
    create_resp = None
    new_token = None
    for attempt in range(2):
        try:
            create_resp, new_token = evolution_client.create_instance(name)
            break
        except _req.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 409) and attempt == 0:
                logger.warning(f"[Recreate] Create {e.response.status_code} on attempt 1 — instance still exists, deleting again")
                evolution_client.delete_instance(name, None)
                time.sleep(3)
                continue
            raise

    if new_token:
        instance.api_token = new_token

        # Extract QR from create response — Evolution v2.3.x includes it immediately
        # in qrcode.base64; older versions send it via QRCODE_UPDATED webhook instead.
        qr_inline = ''
        if create_resp and isinstance(create_resp, dict):
            qr_obj = create_resp.get('qrcode') or {}
            qr_inline = (
                qr_obj.get('base64') or
                create_resp.get('base64') or
                ''
            )
        if qr_inline:
            instance.qr_code = qr_inline
            instance.qr_updated_at = datetime.utcnow()
            logger.info(f"[Recreate] QR saved inline from create response for {name}")
        else:
            logger.info(f"[Recreate] No inline QR — will arrive via QRCODE_UPDATED webhook for {name}")

        db.session.commit()
        logger.info(f"[Recreate] Done — {name} recreated, token saved")
    else:
        logger.error(f"[Recreate] Failed to create instance {name} — no token")


@dashboard_bp.route('/instance/<int:instance_id>/status')
@login_required
def instance_status(instance_id):
    instance = _get_instance(instance_id)
    try:
        state = evolution_client.get_connection_state(instance.instance_name, instance.api_token)
        status_map = {'open': 'connected', 'connecting': 'connecting', 'close': 'disconnected'}
        new_status = status_map.get(state, 'disconnected')
        if instance.status != new_status:
            instance.status = new_status
            db.session.commit()
    except Exception as e:
        logger.debug(f"get_connection_state unavailable: {e}")
    return jsonify({'status': instance.status})


@dashboard_bp.route('/instance/<int:instance_id>/delete', methods=['POST'])
@login_required
def delete_instance(instance_id):
    instance = _get_instance(instance_id)
    try:
        if instance.channel == 'telegram':
            from app.services.telegram import telegram_client
            telegram_client.delete_webhook(instance.api_token)
        else:
            evolution_client.delete_instance(instance.instance_name, instance.api_token)
    except Exception:
        pass
    db.session.delete(instance)
    db.session.commit()
    flash('Instanz gelöscht.', 'success')
    return redirect(url_for('dashboard.index'))


# ─── Bot Config ──────────────────────────────────────────────────────────────

@dashboard_bp.route('/instance/<int:instance_id>/config', methods=['GET', 'POST'])
@login_required
def bot_config(instance_id):
    instance = _get_instance(instance_id)
    config = instance.bot_config

    if config is None:
        config = BotConfig(instance_id=instance_id)
        db.session.add(config)
        db.session.flush()

    if request.method == 'POST':
        config.bot_name = request.form.get('bot_name', 'KI-Assistent').strip()
        config.system_prompt = request.form.get('system_prompt', '').strip()
        config.language = request.form.get('language', 'de')
        try:
            config.max_tokens = int(request.form.get('max_tokens', 500))
        except (ValueError, TypeError):
            config.max_tokens = 500
        config.use_rag = request.form.get('use_rag') == 'on'
        config.is_active = request.form.get('is_active') == 'on'
        # LLM provider / model override
        ai_provider = request.form.get('ai_provider', 'anthropic').strip()
        if ai_provider in ('anthropic', 'mistral', 'local', 'openai'):
            config.ai_provider = ai_provider
        config.ai_model = request.form.get('ai_model', '').strip() or None
        # Normalize notification phone: strip spaces, dashes, leading +
        notif_raw = request.form.get('notification_phone', '').strip()
        notif_clean = ''.join(c for c in notif_raw if c.isdigit())
        config.notification_phone = notif_clean or None
        # Built-in calendar settings
        config.calendar_enabled = request.form.get('calendar_enabled') == 'on'
        try:
            config.business_hours_start = max(0, min(23, int(request.form.get('business_hours_start', 9))))
        except (ValueError, TypeError):
            config.business_hours_start = 9
        try:
            config.business_hours_end = max(1, min(24, int(request.form.get('business_hours_end', 18))))
        except (ValueError, TypeError):
            config.business_hours_end = 18
        try:
            config.appointment_duration = max(15, min(480, int(request.form.get('appointment_duration', 60))))
        except (ValueError, TypeError):
            config.appointment_duration = 60
        tz_raw = request.form.get('calendar_timezone', 'Europe/Berlin').strip()
        if tz_raw in _VALID_TIMEZONES:
            config.calendar_timezone = tz_raw
        db.session.commit()
        flash('Konfiguration gespeichert.', 'success')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    return render_template('dashboard/config.html', instance=instance, config=config)


# ─── Appointments (built-in calendar) ───────────────────────────────────────

@dashboard_bp.route('/instance/<int:instance_id>/appointments')
@login_required
def appointments(instance_id):
    instance = _get_instance(instance_id)
    import zoneinfo
    from datetime import timezone as _tz

    config = instance.bot_config
    tz_name = getattr(config, 'calendar_timezone', 'Europe/Berlin') if config else 'Europe/Berlin'
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo('Europe/Berlin')

    # Filter: status
    status_filter = request.args.get('status', 'upcoming')
    now_utc = datetime.utcnow()

    q = Appointment.query.filter_by(instance_id=instance_id)
    if status_filter == 'upcoming':
        q = q.filter(Appointment.status == 'confirmed', Appointment.start_dt >= now_utc)
    elif status_filter == 'past':
        q = q.filter(Appointment.start_dt < now_utc)
    elif status_filter == 'cancelled':
        q = q.filter(Appointment.status == 'cancelled')
    # else 'all'

    appts_raw = q.order_by(
        Appointment.start_dt.asc() if status_filter == 'upcoming' else Appointment.start_dt.desc()
    ).limit(200).all()

    # Convert to display objects
    appts = []
    for a in appts_raw:
        start_local = a.start_dt.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
        end_local   = a.end_dt.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
        appts.append({
            'id': a.id,
            'customer_name': a.customer_name or '—',
            'customer_phone': a.customer_phone,
            'title': a.title,
            'date': start_local.strftime('%d.%m.%Y'),
            'weekday': ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][start_local.weekday()],
            'time': start_local.strftime('%H:%M'),
            'time_end': end_local.strftime('%H:%M'),
            'note': a.note or '',
            'status': a.status,
        })

    # Stats
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = Appointment.query.filter(
        Appointment.instance_id == instance_id,
        Appointment.status == 'confirmed',
        Appointment.start_dt >= today_start,
        Appointment.start_dt < today_start + timedelta(days=1),
    ).count()
    week_count = Appointment.query.filter(
        Appointment.instance_id == instance_id,
        Appointment.status == 'confirmed',
        Appointment.start_dt >= today_start,
        Appointment.start_dt < today_start + timedelta(days=7),
    ).count()

    return render_template(
        'dashboard/appointments.html',
        instance=instance,
        config=config,
        appointments=appts,
        status_filter=status_filter,
        today_count=today_count,
        week_count=week_count,
        valid_timezones=sorted(_VALID_TIMEZONES),
    )


@dashboard_bp.route('/instance/<int:instance_id>/appointments/<int:appt_id>/cancel', methods=['POST'])
@login_required
def cancel_appointment(instance_id, appt_id):
    _get_instance(instance_id)
    appt = Appointment.query.filter_by(id=appt_id, instance_id=instance_id).first_or_404()
    if appt.status != 'cancelled':
        appt.status = 'cancelled'
        db.session.commit()
        flash(f'Termin "{appt.title}" wurde storniert.', 'success')
    return redirect(url_for('dashboard.appointments', instance_id=instance_id))


# ─── Documents / RAG ─────────────────────────────────────────────────────────

@dashboard_bp.route('/instance/<int:instance_id>/documents')
@login_required
def documents(instance_id):
    instance = _get_instance(instance_id)
    docs = Document.query.filter_by(instance_id=instance_id).order_by(Document.created_at.desc()).all()
    return render_template('dashboard/documents.html', instance=instance, documents=docs)


@dashboard_bp.route('/instance/<int:instance_id>/documents/upload', methods=['POST'])
@login_required
def upload_document(instance_id):
    instance = _get_instance(instance_id)

    if 'file' not in request.files:
        flash('Keine Datei ausgewählt.', 'error')
        return redirect(url_for('dashboard.documents', instance_id=instance_id))

    file = request.files['file']
    if not file.filename or not allowed_file(file.filename):
        flash('Nur PDF, DOCX und TXT Dateien erlaubt.', 'error')
        return redirect(url_for('dashboard.documents', instance_id=instance_id))

    ext = file.filename.rsplit('.', 1)[1].lower()
    if not allowed_file_content(file, ext):
        flash('Dateiinhalt stimmt nicht mit der Dateiendung überein.', 'error')
        return redirect(url_for('dashboard.documents', instance_id=instance_id))

    # Save file
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    upload_folder = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, unique_name)
    file.save(filepath)

    # Create document record
    doc = Document(
        instance_id=instance_id,
        filename=filepath,
        original_name=secure_filename(file.filename),
        file_type=ext,
        file_size=os.path.getsize(filepath),
        status='processing'
    )
    db.session.add(doc)
    db.session.commit()

    # Enable RAG on config (only text documents feed the knowledge base)
    if ext in TEXT_EXTENSIONS and instance.bot_config:
        instance.bot_config.use_rag = True
        db.session.commit()

    # Async processing
    process_document.delay(doc.id)

    flash(f'Datei "{file.filename}" wird verarbeitet...', 'success')
    return redirect(url_for('dashboard.documents', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/documents/status')
@login_required
def documents_status(instance_id):
    """Lightweight JSON poll: id → status for all documents of this instance."""
    _get_instance(instance_id)
    docs = Document.query.filter_by(instance_id=instance_id).all()
    return jsonify({str(d.id): d.status for d in docs})


@dashboard_bp.route('/instance/<int:instance_id>/documents/<int:doc_id>/replace', methods=['POST'])
@login_required
def replace_document(instance_id, doc_id):
    """Replace the file of an existing document and re-process it."""
    _get_instance(instance_id)
    doc = Document.query.filter_by(id=doc_id, instance_id=instance_id).first_or_404()

    file = request.files.get('file')
    if not file or not file.filename or not allowed_file(file.filename):
        flash('Nur PDF, DOCX und TXT Dateien erlaubt.', 'error')
        return redirect(url_for('dashboard.documents', instance_id=instance_id))

    ext = file.filename.rsplit('.', 1)[1].lower()
    if not allowed_file_content(file, ext):
        flash('Dateiinhalt stimmt nicht mit der Dateiendung überein.', 'error')
        return redirect(url_for('dashboard.documents', instance_id=instance_id))

    unique_name = f"{uuid.uuid4().hex}.{ext}"
    upload_folder = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, unique_name)
    file.save(filepath)

    old_path = doc.filename
    try:
        if old_path and os.path.exists(old_path):
            os.remove(old_path)
    except Exception:
        pass

    doc.filename = filepath
    doc.original_name = secure_filename(file.filename)
    doc.file_type = ext
    doc.file_size = os.path.getsize(filepath)
    doc.status = 'processing'
    doc.error_message = None
    doc.chunk_count = 0
    db.session.commit()

    # process_document clears old chunks before re-chunking
    process_document.delay(doc.id)

    flash(f'Datei "{file.filename}" wird ersetzt und neu verarbeitet...', 'success')
    return redirect(url_for('dashboard.documents', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
def delete_document(instance_id, doc_id):
    instance = _get_instance(instance_id)
    doc = Document.query.filter_by(id=doc_id, instance_id=instance_id).first_or_404()

    # Delete file from disk
    try:
        if os.path.exists(doc.filename):
            os.remove(doc.filename)
    except Exception:
        pass

    db.session.delete(doc)
    db.session.commit()
    flash('Dokument gelöscht.', 'success')
    return redirect(url_for('dashboard.documents', instance_id=instance_id))


# ─── Products / Shop ─────────────────────────────────────────────────────────

# Media types for product photos/videos
_PRODUCT_MEDIA = {
    'jpg':  ('image', 'image/jpeg',  b'\xff\xd8\xff'),
    'jpeg': ('image', 'image/jpeg',  b'\xff\xd8\xff'),
    'png':  ('image', 'image/png',   b'\x89PNG'),
    'webp': ('image', 'image/webp',  b'RIFF'),
    'mp4':  ('video', 'video/mp4',   None),   # ftyp box is at offset 4 — checked separately
}


def _check_product_media(file_storage, ext: str) -> bool:
    """Verify magic bytes for product media uploads."""
    if ext == 'mp4':
        header = file_storage.read(12)
        file_storage.seek(0)
        return len(header) >= 12 and header[4:8] == b'ftyp'
    magic = _PRODUCT_MEDIA[ext][2]
    if magic is None:
        return True
    header = file_storage.read(len(magic))
    file_storage.seek(0)
    return header == magic


@dashboard_bp.route('/instance/<int:instance_id>/products')
@login_required
def products(instance_id):
    instance = _get_instance(instance_id)
    items = Product.query.filter_by(instance_id=instance_id).order_by(Product.created_at.desc()).all()
    return render_template('dashboard/products.html', instance=instance, products=items)


@dashboard_bp.route('/instance/<int:instance_id>/products/create', methods=['POST'])
@login_required
def create_product(instance_id):
    _get_instance(instance_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Bitte gib einen Produktnamen ein.', 'error')
        return redirect(url_for('dashboard.products', instance_id=instance_id))

    product = Product(
        instance_id=instance_id,
        name=name[:200],
        price=request.form.get('price', '').strip()[:100],
        description=request.form.get('description', '').strip()[:2000],
    )
    db.session.add(product)
    db.session.commit()
    flash(f'Produkt "{product.name}" angelegt. Füge jetzt Fotos oder Videos hinzu.', 'success')
    return redirect(url_for('dashboard.products', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/products/<int:product_id>/update', methods=['POST'])
@login_required
def update_product(instance_id, product_id):
    _get_instance(instance_id)
    product = Product.query.filter_by(id=product_id, instance_id=instance_id).first_or_404()

    name = request.form.get('name', '').strip()
    if name:
        product.name = name[:200]
    product.price = request.form.get('price', '').strip()[:100]
    product.description = request.form.get('description', '').strip()[:2000]
    product.is_active = request.form.get('is_active') == 'on'
    db.session.commit()
    flash('Produkt aktualisiert.', 'success')
    return redirect(url_for('dashboard.products', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/products/<int:product_id>/delete', methods=['POST'])
@login_required
def delete_product(instance_id, product_id):
    _get_instance(instance_id)
    product = Product.query.filter_by(id=product_id, instance_id=instance_id).first_or_404()

    for m in product.media:
        try:
            if m.filename and os.path.exists(m.filename):
                os.remove(m.filename)
        except Exception:
            pass

    db.session.delete(product)
    db.session.commit()
    flash('Produkt gelöscht.', 'success')
    return redirect(url_for('dashboard.products', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/products/<int:product_id>/media/upload', methods=['POST'])
@login_required
def upload_product_media(instance_id, product_id):
    _get_instance(instance_id)
    product = Product.query.filter_by(id=product_id, instance_id=instance_id).first_or_404()

    file = request.files.get('file')
    if not file or not file.filename or '.' not in file.filename:
        flash('Keine Datei ausgewählt.', 'error')
        return redirect(url_for('dashboard.products', instance_id=instance_id))

    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext not in _PRODUCT_MEDIA:
        flash('Nur JPG, PNG, WEBP (Fotos) und MP4 (Videos) erlaubt.', 'error')
        return redirect(url_for('dashboard.products', instance_id=instance_id))

    if not _check_product_media(file, ext):
        flash('Dateiinhalt stimmt nicht mit der Dateiendung überein.', 'error')
        return redirect(url_for('dashboard.products', instance_id=instance_id))

    media_type, mimetype, _ = _PRODUCT_MEDIA[ext]
    unique_name = f"product_{product_id}_{uuid.uuid4().hex}.{ext}"
    upload_folder = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, unique_name)
    file.save(filepath)

    db.session.add(ProductMedia(
        product_id=product.id,
        filename=filepath,
        original_name=secure_filename(file.filename),
        media_type=media_type,
        mimetype=mimetype,
        file_size=os.path.getsize(filepath),
    ))
    db.session.commit()
    flash(f'{"Video" if media_type == "video" else "Foto"} zu "{product.name}" hinzugefügt.', 'success')
    return redirect(url_for('dashboard.products', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/products/<int:product_id>/media/<int:media_id>/delete', methods=['POST'])
@login_required
def delete_product_media(instance_id, product_id, media_id):
    _get_instance(instance_id)
    media = (
        ProductMedia.query
        .join(Product, ProductMedia.product_id == Product.id)
        .filter(ProductMedia.id == media_id, Product.id == product_id, Product.instance_id == instance_id)
        .first_or_404()
    )
    try:
        if media.filename and os.path.exists(media.filename):
            os.remove(media.filename)
    except Exception:
        pass
    db.session.delete(media)
    db.session.commit()
    flash('Medium gelöscht.', 'success')
    return redirect(url_for('dashboard.products', instance_id=instance_id))


@dashboard_bp.route('/instance/<int:instance_id>/products/<int:product_id>/media/<int:media_id>')
@login_required
def product_media_file(instance_id, product_id, media_id):
    """Serve a product photo/video to the logged-in owner (dashboard thumbnails)."""
    _get_instance(instance_id)
    media = (
        ProductMedia.query
        .join(Product, ProductMedia.product_id == Product.id)
        .filter(ProductMedia.id == media_id, Product.id == product_id, Product.instance_id == instance_id)
        .first_or_404()
    )
    if not media.filename or not os.path.exists(media.filename):
        return jsonify({'error': 'File not found'}), 404
    return send_file(media.filename, mimetype=media.mimetype or 'application/octet-stream')


# ─── Conversations ────────────────────────────────────────────────────────────

@dashboard_bp.route('/instance/<int:instance_id>/conversations')
@login_required
def conversations(instance_id):
    instance = _get_instance(instance_id)
    convs = (
        Conversation.query
        .filter_by(instance_id=instance_id)
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(50)
        .all()
    )
    return render_template('dashboard/conversations.html', instance=instance, conversations=convs)


@dashboard_bp.route('/instance/<int:instance_id>/conversations/<int:conv_id>')
@login_required
def conversation_detail(instance_id, conv_id):
    instance = _get_instance(instance_id)
    conversation = Conversation.query.filter_by(id=conv_id, instance_id=instance_id).first_or_404()
    messages = Message.query.filter_by(conversation_id=conv_id).order_by(Message.created_at).all()
    return render_template(
        'dashboard/conversation_detail.html',
        instance=instance,
        conversation=conversation,
        messages=messages
    )


# ─── Settings ────────────────────────────────────────────────────────────────

@dashboard_bp.route('/settings')
@login_required
def settings():
    from sqlalchemy import func
    total_conversations = (
        db.session.query(func.count(Conversation.id))
        .join(WhatsAppInstance, Conversation.instance_id == WhatsAppInstance.id)
        .filter(WhatsAppInstance.user_id == current_user.id)
        .scalar() or 0
    )
    total_messages = (
        db.session.query(func.count(Message.id))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(WhatsAppInstance, Conversation.instance_id == WhatsAppInstance.id)
        .filter(WhatsAppInstance.user_id == current_user.id)
        .scalar() or 0
    )
    return render_template(
        'dashboard/settings.html',
        total_conversations=total_conversations,
        total_messages=total_messages,
    )


# ─── Helper ───────────────────────────────────────────────────────────────────

def _get_instance(instance_id):
    return WhatsAppInstance.query.filter_by(
        id=instance_id,
        user_id=current_user.id
    ).first_or_404()
