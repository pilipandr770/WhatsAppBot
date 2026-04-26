import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from app import db
from app.models import WhatsAppInstance, Conversation, Message
from app.services.claude_service import get_ai_response
from app.services.rag import search_relevant_chunks
from app.services.evolution import evolution_client
from app.services.stt import transcribe_audio_base64, transcribe_from_evolution

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger(__name__)


@webhook_bp.route('/<instance_name>', methods=['POST'])
def handle_webhook(instance_name):
    """Main webhook endpoint for Evolution API events."""
    data = request.get_json(silent=True)

    if not data:
        return jsonify({'status': 'ok'})

    event = data.get('event', '')
    # Normalize event names: Evolution may send different casings/formats
    normalized_event = (event or '').lower().replace('_', '.')
    logger.info(
        f"WEBHOOK [{instance_name}] event={event!r} normalized={normalized_event!r} "
        f"data_keys={list(data.get('data', {}).keys() if isinstance(data.get('data'), dict) else ['<list>'])}"
    )

    try:
        # Accept several event name formats: dot vs underscore and case variations
        if normalized_event == 'messages.upsert':
            _process_message(instance_name, data)
        elif normalized_event == 'connection.update':
            _process_connection_update(instance_name, data)
        elif normalized_event == 'qrcode.updated':
            _process_qr_update(instance_name, data)
    except Exception as e:
        logger.error(f"Webhook error [{instance_name}] {event}: {e}", exc_info=True)

    return jsonify({'status': 'ok'})


def _process_message(instance_name: str, data: dict):
    msg_data = data.get('data', {})

    # v2.3.7: data may be a list of messages
    if isinstance(msg_data, list):
        for item in msg_data:
            _process_single_message(instance_name, item)
        return

    _process_single_message(instance_name, msg_data)


def _process_single_message(instance_name: str, msg_data: dict):
    key = msg_data.get('key', {})

    # Skip outgoing messages
    if key.get('fromMe', False):
        return

    # Extract text content
    message_obj = msg_data.get('message', {})
    text = (
        message_obj.get('conversation') or
        (message_obj.get('extendedTextMessage') or {}).get('text') or
        ''
    ).strip()

    # Handle voice messages (ptt = push-to-talk / voice note)
    is_voice = False
    if not text:
        audio_msg = message_obj.get('audioMessage', {})
        if audio_msg:
            is_voice = True
            audio_b64 = audio_msg.get('base64', '')
            mime = audio_msg.get('mimetype', 'audio/ogg; codecs=opus')
            logger.info(f"VOICE audio_msg keys={list(audio_msg.keys())} mime={mime} has_b64={bool(audio_b64)}")
            if audio_b64:
                text = transcribe_audio_base64(audio_b64, mime)
            else:
                # Evolution v2.3.x doesn't embed base64 in webhook —
                # fetch media via Evolution's download endpoint
                msg_id = key.get('id', '')
                remote_jid = key.get('remoteJid', '')
                text = transcribe_from_evolution(
                    instance_name=instance_name,
                    token=None,
                    message_id=msg_id,
                    remote_jid=remote_jid,
                    evolution_base_url=evolution_client.base_url,
                    evolution_key=evolution_client.global_key,
                )
            logger.info(f"VOICE transcribed ({len(text)} chars): {text[:80]!r}")

    logger.info(f"MSG key={key} text_len={len(text)} is_voice={is_voice}")

    if not text:
        return  # Skip media, stickers, etc.

    contact_jid = key.get('remoteJid', '')

    # Skip group messages
    if '@g.us' in contact_jid:
        return

    # Find instance in DB
    instance = WhatsAppInstance.query.filter_by(instance_name=instance_name).first()
    if not instance:
        logger.warning(f"Instance not found: {instance_name}")
        return

    config = instance.bot_config
    if not config or not config.is_active:
        return

    # Get or create conversation
    conversation = Conversation.query.filter_by(
        instance_id=instance.id,
        contact_jid=contact_jid
    ).first()

    if not conversation:
        conversation = Conversation(
            instance_id=instance.id,
            contact_jid=contact_jid,
            contact_name=msg_data.get('pushName') or contact_jid.split('@')[0]
        )
        db.session.add(conversation)
        db.session.flush()

    # Save incoming message (mark voice transcriptions for clarity in history)
    stored_content = f'[Sprachnachricht]: {text}' if is_voice else text
    user_message = Message(
        conversation_id=conversation.id,
        role='user',
        content=stored_content
    )
    db.session.add(user_message)
    db.session.flush()

    # Build conversation history (last 10 messages for context window)
    history_msgs = (
        Message.query
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.created_at.desc())
        .limit(10)
        .all()
    )
    history = [{'role': m.role, 'content': m.content} for m in reversed(history_msgs)]

    # RAG context
    rag_context = None
    if config.use_rag:
        rag_context = search_relevant_chunks(instance.id, text)

    # Generate AI response (add voice hint to system prompt if needed)
    system_prompt = config.system_prompt
    if is_voice:
        system_prompt = (
            system_prompt +
            '\n\nHINWEIS: Die letzte Nachricht des Nutzers war eine Sprachnachricht '
            '(automatisch transkribiert). Antworte normal, erwähne die Transkription nicht.'
        )

    ai_text = get_ai_response(
        system_prompt=system_prompt,
        messages=history,
        rag_context=rag_context,
        max_tokens=config.max_tokens
    )

    # Save AI response
    assistant_message = Message(
        conversation_id=conversation.id,
        role='assistant',
        content=ai_text
    )
    db.session.add(assistant_message)

    # Update conversation stats
    conversation.message_count = (conversation.message_count or 0) + 2
    conversation.last_message_at = datetime.utcnow()

    db.session.commit()

    # Send reply via Evolution API
    evolution_client.send_text(
        instance_name=instance_name,
        token=instance.api_token,
        to_jid=contact_jid,
        text=ai_text
    )

    logger.info(f"Replied to {contact_jid} on {instance_name}: {len(ai_text)} chars")


def _process_qr_update(instance_name: str, data: dict):
    qr_data = data.get('data', {})

    # Try all known payload formats across Evolution API versions:
    # v2.2.x: data.qrcode.base64
    # v2.3.x: data.base64  (direct)
    # fallback: data.qr
    qr_base64 = (
        qr_data.get('base64') or
        (qr_data.get('qrcode') or {}).get('base64') or
        qr_data.get('qr') or
        ''
    )

    if not qr_base64:
        # Log full structure (truncated) to help diagnose format changes
        import json as _json
        try:
            preview = _json.dumps(qr_data)[:300]
        except Exception:
            preview = str(qr_data)[:300]
        logger.warning(f"QRCODE_UPDATED for {instance_name}: no base64 found. data={preview}")
        return

    instance = WhatsAppInstance.query.filter_by(instance_name=instance_name).first()
    if not instance:
        logger.warning(f"QRCODE_UPDATED: instance not found in DB: {instance_name}")
        return

    instance.qr_code = qr_base64
    instance.qr_updated_at = datetime.utcnow()
    db.session.commit()
    logger.info(f"✅ QR stored for {instance_name} ({len(qr_base64)} bytes)")


def _process_connection_update(instance_name: str, data: dict):
    state = data.get('data', {}).get('state', '')
    instance = WhatsAppInstance.query.filter_by(instance_name=instance_name).first()

    if not instance:
        return

    state_map = {
        'open': 'connected',
        'connecting': 'connecting',
        'close': 'disconnected',
    }

    new_status = state_map.get(state, instance.status)
    if instance.status != new_status:
        instance.status = new_status
        db.session.commit()
        logger.info(f"Instance {instance_name} status → {new_status}")
