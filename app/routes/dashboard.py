import os
import uuid
import logging
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
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

    # Return QR stored from webhook if fresh (< 55 seconds old)
    if instance.qr_code and instance.qr_updated_at:
        from datetime import datetime, timezone
        age = (datetime.utcnow() - instance.qr_updated_at).total_seconds()
        if age < 55:
            return jsonify({'qr': instance.qr_code, 'status': instance.status})
        else:
            # QR expired — clear it so frontend knows to wait for new one
            instance.qr_code = None
            db.session.commit()

    # No fresh QR in DB — trigger connect asynchronously (fire and forget)
    try:
        import threading
        def _trigger_connect():
            try:
                evolution_client.trigger_connect(instance.instance_name, instance.api_token)
            except Exception as ex:
                logger.debug(f"trigger_connect: {ex}")
        t = threading.Thread(target=_trigger_connect, daemon=True)
        t.start()
    except Exception as e:
        logger.warning(f"Could not start connect thread: {e}")

    return jsonify({'qr': '', 'status': instance.status})


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
