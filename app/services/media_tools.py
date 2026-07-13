"""
Bot tool: send knowledge-base files (price list PDF, product photos)
directly to the customer in WhatsApp via Evolution sendMedia.

The list of available files is injected into the system prompt;
Claude calls send_file_to_customer with the file name when the
customer asks for a price list, photos, brochure etc.
"""

import os
import logging

logger = logging.getLogger(__name__)

# WhatsApp media limit safety cap (Evolution/Baileys handles up to ~100 MB,
# but base64 through webhook worker memory — keep it sane)
MAX_SEND_BYTES = 25 * 1024 * 1024

_MIMETYPES = {
    'pdf':  'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'txt':  'text/plain',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
}

_IMAGE_TYPES = {'jpg', 'jpeg', 'png'}


MEDIA_TOOLS = [
    {
        "name": "send_file_to_customer",
        "description": (
            "Sendet eine Datei aus der Wissensdatenbank (z. B. Preisliste als PDF, "
            "Produktfoto, Broschüre) direkt an den Kunden in WhatsApp. "
            "Nutze dieses Tool, wenn der Kunde nach einer Preisliste, Fotos, "
            "Unterlagen oder Dokumenten fragt und eine passende Datei verfügbar ist. "
            "Gib den exakten Dateinamen aus der Liste der verfügbaren Dateien an."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Exakter Name der Datei aus der Liste der verfügbaren Dateien"
                },
                "caption": {
                    "type": "string",
                    "description": "Optionale kurze Bildunterschrift/Begleittext für die Datei"
                }
            },
            "required": ["filename"]
        }
    }
]


def get_sendable_documents(instance_id: int):
    """Ready documents of this instance that exist on disk."""
    from app.models import Document
    docs = Document.query.filter_by(instance_id=instance_id, status='ready').all()
    return [d for d in docs if d.filename and os.path.exists(d.filename)]


def build_files_hint(docs) -> str:
    """System-prompt block listing files the bot may send."""
    if not docs:
        return ''
    lines = '\n'.join(
        f"- {d.original_name} ({d.file_type.upper()}, {round((d.file_size or 0) / 1024)} KB)"
        for d in docs
    )
    return (
        "\n\nVERFÜGBARE DATEIEN (per send_file_to_customer an den Kunden sendbar):\n"
        f"{lines}\n"
        "Sende eine Datei nur, wenn der Kunde danach fragt oder sie eindeutig hilfreich ist. "
        "Erfinde keine Dateinamen — nutze nur Namen aus dieser Liste."
    )


def send_file_to_customer(instance, contact_jid: str, tool_input: dict) -> str:
    """Execute the tool: look up the document and send it on the instance's channel."""
    from app import db
    from app.models import BotEvent
    from app.services import messaging

    filename = (tool_input.get('filename') or '').strip()
    caption = (tool_input.get('caption') or '').strip()[:300]
    if not filename:
        return "Fehler: kein Dateiname angegeben."

    docs = get_sendable_documents(instance.id)
    doc = next((d for d in docs if d.original_name == filename), None)
    if doc is None:
        # forgiving match: case-insensitive
        doc = next((d for d in docs if (d.original_name or '').lower() == filename.lower()), None)
    if doc is None:
        available = ', '.join(d.original_name for d in docs) or 'keine'
        return f"Datei '{filename}' nicht gefunden. Verfügbare Dateien: {available}"

    size = os.path.getsize(doc.filename)
    if size > MAX_SEND_BYTES:
        return f"Datei '{filename}' ist zu groß zum Versenden ({round(size / 1024 / 1024)} MB)."

    with open(doc.filename, 'rb') as f:
        media_bytes = f.read()

    mediatype = 'image' if doc.file_type in _IMAGE_TYPES else 'document'
    mimetype = _MIMETYPES.get(doc.file_type, 'application/octet-stream')

    try:
        messaging.send_media(
            instance=instance,
            to_id=contact_jid,
            media_bytes=media_bytes,
            kind=mediatype,
            mimetype=mimetype,
            filename=doc.original_name,
            caption=caption,
        )
    except Exception as e:
        logger.error(f"send_file_to_customer failed ({filename} → {contact_jid}): {e}")
        return f"Senden der Datei '{filename}' fehlgeschlagen. Bitte später erneut versuchen."

    db.session.add(BotEvent(
        instance_id=instance.id,
        event_type='media_sent',
        contact_jid=contact_jid,
        meta=doc.original_name,
    ))
    db.session.commit()

    logger.info(f"Media sent: {doc.original_name} ({mediatype}) → {contact_jid}")
    return (
        f"Datei '{doc.original_name}' wurde erfolgreich an den Kunden gesendet. "
        "Bestätige dem Kunden kurz, dass die Datei unterwegs ist."
    )
