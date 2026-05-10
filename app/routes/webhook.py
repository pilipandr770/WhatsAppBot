import os
import re
import json
import hashlib
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from app import db
from app.models import WhatsAppInstance, Conversation, Message, SiteConfig
from app.services.claude_service import get_ai_response, get_ai_response_with_tools
from app.services.rag import search_relevant_chunks
from app.services.evolution import evolution_client
from app.services.stt import transcribe_audio_base64, transcribe_from_evolution
from app.services.google_service import GOOGLE_TOOLS, execute_tool as google_execute_tool

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger(__name__)

_APPOINTMENT_MARKER_RE = re.compile(r"\[\[APPOINTMENT_NOTIFY\|(.+?)\]\]", re.DOTALL)
_APPOINTMENT_DEDUP_MINUTES = int(os.environ.get('APPOINTMENT_NOTIFY_DEDUP_MINUTES', '30'))

# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------

def _verify_webhook_token() -> bool:
    """
    Verify that the incoming request carries the Evolution API global key.
    Evolution sends it as the 'apikey' header.
    If EVOLUTION_WEBHOOK_TOKEN is not set we skip the check.
    """
    expected = os.environ.get('EVOLUTION_WEBHOOK_TOKEN', '')
    if not expected:
        return True  # Token not configured — allow all (disable check)

    # Evolution API v2 sends the global key as 'apikey' header
    provided = (
        request.headers.get('apikey') or
        request.headers.get('Authorization', '').replace('Bearer ', '') or
        ''
    )

    if provided != expected:
        # Log ALL headers so we can see what Evolution actually sends
        safe_headers = {
            k: v for k, v in request.headers
            if k.lower() not in ('cookie', 'authorization')
        }
        logger.warning(
            f"Webhook auth failed. Expected token present: {bool(expected)}. "
            f"Received headers: {safe_headers}"
        )
        return False
    return True


@webhook_bp.route('/<instance_name>', methods=['POST'])
def handle_webhook(instance_name):
    """Main webhook endpoint for Evolution API events."""
    if not _verify_webhook_token():
        logger.warning(f"Webhook [{instance_name}]: unauthorized from {request.remote_addr}")
        return jsonify({'error': 'Unauthorized'}), 401

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

    # Inject current date/time so Claude can resolve relative times ("tomorrow", "next Monday")
    from datetime import timezone as _tz
    import zoneinfo
    _local_tz = zoneinfo.ZoneInfo(os.environ.get('CALENDAR_TIMEZONE', 'Europe/Berlin'))
    _now = datetime.now(_local_tz)
    _datetime_hint = (
        f"\n\nAktuelles Datum und Uhrzeit: "
        f"{_now.strftime('%A, %d.%m.%Y %H:%M')} "
        f"(Zeitzone: {os.environ.get('CALENDAR_TIMEZONE', 'Europe/Berlin')}). "
        "Verwende diese Information, wenn Terminzeiten berechnet werden müssen."
    )
    _appointment_hint = (
        "\n\nTERMIN-HINWEIS (WICHTIG): "
        "Wenn der Kunde einen NEUEN Termin mit konkreter Zeit verbindlich bestätigt, "
        "füge am ENDE deiner Antwort genau eine technische Marker-Zeile hinzu: "
        "[[APPOINTMENT_NOTIFY|title=<kurzer Titel>;datetime=<Datum/Zeit>;note=<optional>]]. "
        "Nur bei echter Termin-Bestätigung verwenden. "
        "Niemals Google-Kalender schreiben, bearbeiten oder löschen."
    )
    system_prompt = system_prompt + _datetime_hint + _appointment_hint

    # Use Google tools if this instance has an active Google token
    has_google = bool(instance.google_token and instance.google_token.access_token)

    # Collect write-tool events for owner notifications
    write_events: list = []

    # Tools that count as "write" operations → trigger owner notification
    _WRITE_TOOLS = {'google_calendar_create_event', 'google_sheets_append'}

    if has_google:
        _inst_id = instance.id  # capture for closure

        def _tool_executor(tool_name: str, tool_input: dict) -> str:
            result = google_execute_tool(tool_name, tool_input, _inst_id)
            # Track successful write operations (result starts with ✅)
            if tool_name in _WRITE_TOOLS and result.startswith('✅'):
                write_events.append({'tool': tool_name, 'input': tool_input, 'result': result})
            return result

        # Ensure enough tokens for tool-use reasoning + final answer (min 1500)
        ai_text_raw = get_ai_response_with_tools(
            system_prompt=system_prompt,
            messages=history,
            tools=GOOGLE_TOOLS,
            tool_executor=_tool_executor,
            rag_context=rag_context,
            max_tokens=max(config.max_tokens, 1500)
        )
    else:
        ai_text_raw = get_ai_response(
            system_prompt=system_prompt,
            messages=history,
            rag_context=rag_context,
            max_tokens=config.max_tokens
        )

    appointment_event, ai_text = _extract_appointment_event(ai_text_raw)

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

    # ── Owner notifications for write-tool events ─────────────────────────────
    if write_events and config.notification_phone:
        owner_jid = f"{config.notification_phone}@s.whatsapp.net"
        contact_display = conversation.contact_name or contact_jid.split('@')[0]
        customer_phone  = contact_jid.split('@')[0]

        for ev in write_events:
            notif = _build_owner_notification(ev, contact_display, customer_phone)
            try:
                evolution_client.send_text(
                    instance_name=instance_name,
                    token=instance.api_token,
                    to_jid=owner_jid,
                    text=notif
                )
                logger.info(
                    f"Owner notification sent to {owner_jid} "
                    f"for {ev['tool']} on {instance_name}"
                )
            except Exception as notif_err:
                logger.warning(f"Owner notification failed: {notif_err}")

    # ── Owner notification for appointment intent (no Google write) ───────────
    if appointment_event and config.notification_phone:
        owner_jid = f"{config.notification_phone}@s.whatsapp.net"
        contact_display = conversation.contact_name or contact_jid.split('@')[0]
        customer_phone = contact_jid.split('@')[0]
        fingerprint = _appointment_fingerprint(appointment_event, customer_phone)
        if _should_send_appointment_notify(instance.id, customer_phone, fingerprint):
            notif = _build_appointment_notify_message(appointment_event, contact_display, customer_phone)
            try:
                evolution_client.send_text(
                    instance_name=instance_name,
                    token=instance.api_token,
                    to_jid=owner_jid,
                    text=notif
                )
                _mark_appointment_notify_sent(instance.id, customer_phone, fingerprint)
                logger.info(f"Owner appointment notification sent to {owner_jid} on {instance_name}")
            except Exception as notif_err:
                logger.warning(f"Owner appointment notification failed: {notif_err}")
        else:
            logger.info(
                "Skipped duplicate appointment notification for instance=%s customer=%s",
                instance.id,
                customer_phone,
            )


def _extract_appointment_event(ai_text: str):
    """
    Extract [[APPOINTMENT_NOTIFY|...]] marker from model output.
    Returns: (event_dict_or_none, cleaned_text_for_customer)
    """
    if not ai_text:
        return None, ai_text

    match = _APPOINTMENT_MARKER_RE.search(ai_text)
    if not match:
        return None, ai_text

    payload = match.group(1)
    event = {'title': 'Neuer Termin', 'datetime': '—', 'note': ''}

    for part in payload.split(';'):
        if '=' not in part:
            continue
        key, val = part.split('=', 1)
        key = key.strip().lower()
        val = val.strip()
        if key in event and val:
            event[key] = val

    cleaned = _APPOINTMENT_MARKER_RE.sub('', ai_text).strip()
    return event, cleaned


def _build_appointment_notify_message(event: dict, contact_name: str, customer_phone: str) -> str:
    """Build owner WhatsApp notification for a confirmed new appointment request."""
    title = event.get('title', 'Neuer Termin')
    dt = event.get('datetime', '—')
    note = event.get('note', '')

    lines = [
        "📅 *Neuer Termin bestätigt (manuell eintragen)*",
        "",
        f"👤 Kunde: {contact_name}",
        f"📞 Nummer: +{customer_phone}",
        f"📌 Termin: {title}",
        f"🕐 Zeit: {dt}",
    ]
    if note:
        lines.append(f"📝 Notiz: {note}")

    lines += ["", "— dein WhatsApp Bot 🤖"]
    return '\n'.join(lines)


def _appointment_dedup_key(instance_id: int, customer_phone: str) -> str:
    return f"appt_notify:{instance_id}:{customer_phone}"


def _appointment_fingerprint(event: dict, customer_phone: str) -> str:
    raw = '|'.join([
        customer_phone.strip(),
        (event.get('title') or '').strip().lower(),
        (event.get('datetime') or '').strip().lower(),
        (event.get('note') or '').strip().lower(),
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _should_send_appointment_notify(instance_id: int, customer_phone: str, fingerprint: str) -> bool:
    """Return True if this appointment notification should be sent (not a recent duplicate)."""
    key = _appointment_dedup_key(instance_id, customer_phone)
    raw = SiteConfig.get(key, '')
    if not raw:
        return True

    try:
        data = json.loads(raw)
        last_fp = data.get('fingerprint', '')
        last_ts_raw = data.get('sent_at', '')
        if not last_fp or not last_ts_raw:
            return True

        last_ts = datetime.fromisoformat(last_ts_raw)
        too_recent = datetime.utcnow() - last_ts < timedelta(minutes=_APPOINTMENT_DEDUP_MINUTES)
        return not (too_recent and last_fp == fingerprint)
    except Exception:
        return True


def _mark_appointment_notify_sent(instance_id: int, customer_phone: str, fingerprint: str):
    key = _appointment_dedup_key(instance_id, customer_phone)
    payload = {
        'fingerprint': fingerprint,
        'sent_at': datetime.utcnow().isoformat(),
    }
    SiteConfig.set(key, json.dumps(payload))
    db.session.commit()


def _build_owner_notification(event: dict, contact_name: str, customer_phone: str) -> str:
    """Build a human-readable WhatsApp notification for the business owner."""
    tool  = event['tool']
    inp   = event['input']

    if tool == 'google_calendar_create_event':
        summary  = inp.get('summary', '—')
        start    = inp.get('start_datetime', '—')
        end      = inp.get('end_datetime', '—')
        desc     = inp.get('description', '')
        lines = [
            "📅 *Neuer Termin gebucht!*",
            "",
            f"👤 Kunde: {contact_name}",
            f"📞 Nummer: +{customer_phone}",
            f"📌 Termin: {summary}",
            f"🕐 Start: {start}",
            f"🕑 Ende:  {end}",
        ]
        if desc:
            lines.append(f"📝 Notiz: {desc}")
        lines += ["", "— dein WhatsApp Bot 🤖"]
        return '\n'.join(lines)

    elif tool == 'google_sheets_append':
        values = inp.get('values', [])
        value_str = ' | '.join(str(v) for v in values) if values else '—'
        return (
            f"📋 *Neuer Eintrag gespeichert!*\n\n"
            f"👤 Kunde: {contact_name}\n"
            f"📞 Nummer: +{customer_phone}\n"
            f"📊 Daten: {value_str}\n\n"
            "— dein WhatsApp Bot 🤖"
        )

    # Fallback for other write tools
    return (
        f"✅ *Aktion ausgeführt: {tool}*\n\n"
        f"👤 Kunde: {contact_name} (+{customer_phone})\n\n"
        "— dein WhatsApp Bot 🤖"
    )


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
