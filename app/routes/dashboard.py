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
from app.models import WhatsAppInstance, BotConfig, Document, Conversation, Message, Subscription
from app.services.evolution import evolution_client
from app.tasks import process_document

dashboard_bp = Blueprint('dashboard', __name__)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}

# Magic bytes: first bytes that identify a file type regardless of extension
_MAGIC_BYTES = {
    'pdf':  b'%PDF',
    'docx': b'PK\x03\x04',   # ZIP-based (Office Open XML)
    'txt':  None,              # plain text has no magic bytes — skip check
}


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

    # No fresh QR — poke Evolution API to (re)generate one via trigger_connect.
    # The QRCODE_UPDATED webhook will deliver the QR asynchronously.
    # Full delete+create (recreate) is only done via the manual "Neu verbinden" button
    # to avoid race conditions with concurrent reconnect attempts.
    _inst_name = instance.instance_name
    _inst_token = instance.api_token

    def _do_trigger():
        try:
            evolution_client.trigger_connect(_inst_name, _inst_token)
            logger.debug(f"[QR] trigger_connect sent for {_inst_name}")
        except Exception as ex:
            logger.debug(f"[QR] trigger_connect {_inst_name}: {ex}")

    threading.Thread(target=_do_trigger, daemon=True).start()

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
    """Delete + re-create the Evolution API instance so it fires a fresh QRCODE_UPDATED webhook.

    Ordering:
    1. Delete (global key, ignores errors).
    2. sleep(3) for Evolution to finish cleanup.
    3. Create — retry once with another delete if 403 (instance still exists).
    4. Update ONLY api_token in DB — never touch qr_code/qr_updated_at after create().
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
    new_token = None
    for attempt in range(2):
        try:
            _, new_token = evolution_client.create_instance(name)
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
        db.session.commit()
        logger.info(f"[Recreate] Done — {name} recreated, token saved, awaiting QRCODE_UPDATED webhook")
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
