"""
Bot tool: product catalog awareness + sending product photos/videos.

The active catalog (name, price, description, media counts) is injected
into the system prompt, so the bot can name prices and describe products.
When a customer asks for photos or a video, Claude calls send_product_media
and the files go out via Evolution sendMedia.
"""

import os
import logging

logger = logging.getLogger(__name__)

MAX_PROMPT_PRODUCTS = 50      # cap catalog size in the system prompt
MAX_DESC_CHARS = 250          # description excerpt per product in the prompt
MAX_ITEMS_PER_SEND = 5        # never blast more media than this per tool call
MAX_SEND_BYTES = 25 * 1024 * 1024


PRODUCT_TOOLS = [
    {
        "name": "send_product_media",
        "description": (
            "Sendet Fotos oder ein Video eines Produkts aus dem Katalog direkt an den "
            "Kunden in WhatsApp. Nutze dieses Tool, wenn der Kunde Fotos, Bilder oder "
            "ein Video zu einem konkreten Produkt sehen möchte. "
            "Gib den exakten Produktnamen aus dem Katalog an."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "Exakter Name des Produkts aus dem Katalog"
                },
                "media_type": {
                    "type": "string",
                    "enum": ["photos", "video", "all"],
                    "description": "Was senden: Fotos, Video oder beides (Standard: photos)"
                },
                "count": {
                    "type": "integer",
                    "description": f"Wie viele Fotos senden (Standard 3, max. {MAX_ITEMS_PER_SEND})"
                }
            },
            "required": ["product_name"]
        }
    }
]


def get_active_products(instance_id: int):
    from app.models import Product
    return (
        Product.query
        .filter_by(instance_id=instance_id, is_active=True)
        .order_by(Product.created_at.desc())
        .limit(MAX_PROMPT_PRODUCTS)
        .all()
    )


def build_catalog_hint(products) -> str:
    """System-prompt block: the catalog the bot knows and can talk about."""
    if not products:
        return ''
    lines = []
    for p in products:
        desc = (p.description or '').strip().replace('\n', ' ')
        if len(desc) > MAX_DESC_CHARS:
            desc = desc[:MAX_DESC_CHARS] + '…'
        parts = [f"• {p.name}"]
        if p.price:
            parts.append(f"Preis: {p.price}")
        if desc:
            parts.append(desc)
        media_bits = []
        if p.photo_count:
            media_bits.append(f"{p.photo_count} Fotos")
        if p.video_count:
            media_bits.append(f"{p.video_count} Videos")
        if media_bits:
            parts.append(f"[{', '.join(media_bits)} verfügbar — per send_product_media sendbar]")
        lines.append(' | '.join(parts))

    return (
        "\n\nPRODUKTKATALOG (aktuelle Produkte/Dienstleistungen mit Preisen):\n"
        + '\n'.join(lines) +
        "\nNenne Preise und Details direkt aus diesem Katalog. "
        "Wenn der Kunde Fotos oder ein Video möchte, nutze send_product_media. "
        "Erfinde keine Produkte oder Preise, die hier nicht stehen."
    )


def _find_product(products, name: str):
    name_l = (name or '').strip().lower()
    if not name_l:
        return None
    # exact (case-insensitive) → substring match
    for p in products:
        if p.name.lower() == name_l:
            return p
    for p in products:
        if name_l in p.name.lower() or p.name.lower() in name_l:
            return p
    return None


def send_product_media(instance, contact_jid: str, tool_input: dict) -> str:
    """Execute the tool: send photos and/or video of a product to the customer."""
    from app import db
    from app.models import BotEvent
    from app.services import messaging

    products = get_active_products(instance.id)
    product = _find_product(products, tool_input.get('product_name', ''))
    if product is None:
        available = ', '.join(p.name for p in products) or 'keine'
        return f"Produkt nicht gefunden. Verfügbare Produkte: {available}"

    media_type = tool_input.get('media_type') or 'photos'
    try:
        count = int(tool_input.get('count') or 3)
    except (TypeError, ValueError):
        count = 3
    count = max(1, min(count, MAX_ITEMS_PER_SEND))

    images = [m for m in product.media if m.media_type == 'image']
    videos = [m for m in product.media if m.media_type == 'video']

    to_send = []
    if media_type in ('photos', 'all'):
        to_send += images[:count]
    if media_type in ('video', 'all'):
        to_send += videos[:1]
    if media_type == 'video' and not videos:
        return f"Für '{product.name}' ist kein Video hinterlegt ({len(images)} Fotos verfügbar)."
    if not to_send:
        return f"Für '{product.name}' sind keine Medien hinterlegt."

    caption_base = product.name + (f" — {product.price}" if product.price else "")
    sent = 0
    for idx, m in enumerate(to_send):
        if not m.filename or not os.path.exists(m.filename):
            continue
        if os.path.getsize(m.filename) > MAX_SEND_BYTES:
            continue
        with open(m.filename, 'rb') as f:
            media_bytes = f.read()
        try:
            messaging.send_media(
                instance=instance,
                to_id=contact_jid,
                media_bytes=media_bytes,
                kind='image' if m.media_type == 'image' else 'video',
                mimetype=m.mimetype or 'application/octet-stream',
                filename=m.original_name or f"{product.name}.{(m.filename or '').rsplit('.', 1)[-1]}",
                caption=caption_base if idx == 0 else '',
            )
            sent += 1
        except Exception as e:
            logger.error(f"send_product_media {product.name} → {contact_jid}: {e}")

    if not sent:
        return f"Senden der Medien für '{product.name}' fehlgeschlagen. Bitte später erneut versuchen."

    try:
        db.session.add(BotEvent(
            instance_id=instance.id,
            event_type='media_sent',
            contact_jid=contact_jid,
            meta=f"{product.name} ({sent} Medien)",
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()

    remaining = max(0, len(images) - count) if media_type in ('photos', 'all') else len(images)
    note = f" Es gibt noch {remaining} weitere Fotos." if remaining > 0 else ""
    logger.info(f"Product media sent: {product.name} x{sent} → {contact_jid}")
    return (
        f"{sent} Medien zu '{product.name}' wurden an den Kunden gesendet.{note} "
        "Bestätige dem Kunden kurz und nenne ggf. den Preis."
    )
