import os
import uuid
import time
import threading
import logging
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app import db
from app.models import WhatsAppInstance, BotConfig, Document, Conversation, Message
from app.services.evolution import evolution_client
from app.tasks import process_document

dashboard_bp = Blueprint('dashboard', __name__)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ─── Main dashboard ──────────────────────────────────────────────────────────

@dashboard_bp.route('/')
@login_required
def index():
    instances = WhatsAppInstance.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard/index.html', instances=instances)


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
    return render_template('dashboard/connect.html', instance=instance)


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

    # No fresh QR in DB.
    # Use qr_updated_at as a DB-level cooldown marker (works across multiple Gunicorn workers):
    #   - None          → never attempted a recreate
    #   - set, qr=None  → recreate triggered recently, webhook not yet received
    #   - set, qr!=None → QR received (handled above)
    now = datetime.utcnow()
    instance_age = (now - instance.created_at).total_seconds() if instance.created_at else 999
    last_attempt_age = (
        (now - instance.qr_updated_at).total_seconds()
        if instance.qr_updated_at else None
    )

    # Trigger a new recreate if:
    #   - instance is old enough (> 30s) AND
    #   - never attempted before, OR last attempt was > 90s ago (previous one failed)
    needs_recreate = (
        instance_age > 30 and
        (last_attempt_age is None or last_attempt_age > 90)
    )

    if needs_recreate:
        logger.info(
            f"[QR] Scheduling recreate for {instance.instance_name} "
            f"(age={instance_age:.0f}s, last_attempt={last_attempt_age})"
        )
        # Stamp qr_updated_at NOW as a "recreate pending" marker BEFORE spawning thread.
        # This prevents other workers from also triggering a recreate within 90s.
        instance.qr_updated_at = now
        instance.qr_code = None
        db.session.commit()

        app = current_app._get_current_object()

        def _do_recreate():
            with app.app_context():
                inst = db.session.get(WhatsAppInstance, instance_id)
                if inst:
                    _recreate_evolution_instance(inst)

        threading.Thread(target=_do_recreate, daemon=True).start()
    else:
        logger.debug(f"[QR] Waiting for webhook: {instance.instance_name} last_attempt={last_attempt_age:.0f}s ago"
                     if last_attempt_age else f"[QR] Instance too young: {instance.instance_name}")

    return jsonify({'qr': '', 'status': instance.status})


@dashboard_bp.route('/instance/<int:instance_id>/reconnect', methods=['POST'])
@login_required
def reconnect_instance(instance_id):
    """Force-delete and re-create the Evolution API instance to get a fresh QR."""
    instance = _get_instance(instance_id)
    if instance.status == 'connected':
        return jsonify({'status': 'ok', 'message': 'Already connected'})
    try:
        # Stamp pending marker before recreate (same pattern as get_qr auto-recreate)
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
    """Delete + re-create the Evolution API instance so it fires a fresh QRCODE_UPDATED webhook.

    IMPORTANT ordering:
    1. Delete old Evolution instance.
    2. Clear DB state (qr_code=None) and COMMIT — so the incoming QRCODE_UPDATED webhook
       writes to a clean slate without being overwritten afterward.
    3. Create new Evolution instance → Evolution fires QRCODE_UPDATED webhook immediately.
    4. Update ONLY the token in DB — never touch qr_code/qr_updated_at after create()
       because the webhook handler may already have stored the fresh QR by then.
    """
    name = instance.instance_name
    logger.info(f"[Recreate] Deleting Evolution instance {name}")
    try:
        # Use global API key for delete — avoids token mismatch (stored token may be stale)
        evolution_client.delete_instance(name, None)  # None → falls back to global_key
    except Exception as e:
        logger.warning(f"[Recreate] delete_instance {name}: {e}")

    time.sleep(2)  # give Evolution API a moment to clean up

    # Update status — qr_updated_at was already stamped in get_qr() before this thread started
    instance.status = 'connecting'
    db.session.commit()

    logger.info(f"[Recreate] Creating Evolution instance {name}")
    _, new_token = evolution_client.create_instance(name)

    # Only update the token — do NOT touch qr_code or qr_updated_at.
    # The QRCODE_UPDATED webhook from create_instance() may already have arrived and
    # stored the fresh QR. Overwriting here would cause the "QR disappears" race condition.
    instance.api_token = new_token
    db.session.commit()
    logger.info(f"[Recreate] Done — {name} token updated, awaiting QRCODE_UPDATED webhook")


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
        db.session.commit()
        flash('Konfiguration gespeichert.', 'success')
        return redirect(url_for('dashboard.bot_config', instance_id=instance_id))

    return render_template('dashboard/config.html', instance=instance, config=config)


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

    # Save file
    ext = file.filename.rsplit('.', 1)[1].lower()
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

    # Enable RAG on config
    if instance.bot_config:
        instance.bot_config.use_rag = True
        db.session.commit()

    # Async processing
    process_document.delay(doc.id)

    flash(f'Datei "{file.filename}" wird verarbeitet...', 'success')
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


# ─── Helper ───────────────────────────────────────────────────────────────────

def _get_instance(instance_id):
    return WhatsAppInstance.query.filter_by(
        id=instance_id,
        user_id=current_user.id
    ).first_or_404()
