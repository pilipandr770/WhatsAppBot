"""
Internal Calendar Service — AI-driven appointment scheduling.

Provides CALENDAR_TOOLS (Anthropic format) and execute_tool() dispatcher.
The AI creates/cancels appointments directly via tools; owner receives
an instant WhatsApp notification on every change.
"""

import logging
import re
from datetime import datetime, timedelta

import zoneinfo

logger = logging.getLogger(__name__)

# ── Validation helpers ────────────────────────────────────────────────────────

_ISO_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)?$'
)
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# ── AI Tool Definitions ───────────────────────────────────────────────────────

CALENDAR_TOOLS = [
    {
        "name": "calendar_check_slots",
        "description": (
            "Gibt freie Terminslots für einen bestimmten Tag zurück. "
            "Nutze dieses Tool wenn ein Kunde nach verfügbaren Terminen fragt. "
            "Das Tool berücksichtigt bereits gebuchte Termine und die Geschäftszeiten automatisch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Datum im Format YYYY-MM-DD, z. B. 2026-07-15"
                }
            },
            "required": ["date"]
        }
    },
    {
        "name": "calendar_create_appointment",
        "description": (
            "Erstellt einen neuen Termin. Nur aufrufen wenn der Kunde Datum und Uhrzeit "
            "klar bestätigt hat. Prüfe vorher mit calendar_check_slots ob der Slot frei ist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Name des Kunden"
                },
                "title": {
                    "type": "string",
                    "description": "Art des Termins, z. B. 'Beratung', 'Haarschnitt', 'Besichtigung'"
                },
                "start_datetime": {
                    "type": "string",
                    "description": "Startzeit im Format YYYY-MM-DDTHH:MM, z. B. 2026-07-15T10:00"
                },
                "note": {
                    "type": "string",
                    "description": "Optionale Anmerkung zum Termin"
                }
            },
            "required": ["customer_name", "title", "start_datetime"]
        }
    },
    {
        "name": "calendar_cancel_appointment",
        "description": (
            "Storniert einen bestehenden Termin anhand der Termin-ID. "
            "Nutze calendar_list_upcoming um die ID zu finden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "integer",
                    "description": "ID des zu stornierenden Termins"
                },
                "reason": {
                    "type": "string",
                    "description": "Optionaler Stornierungsgrund"
                }
            },
            "required": ["appointment_id"]
        }
    },
    {
        "name": "calendar_list_upcoming",
        "description": (
            "Listet bevorstehende Termine des aktuellen Kunden auf. "
            "Nutze dieses Tool wenn der Kunde nach seinen Terminen fragt oder stornieren möchte."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Zeitraum in Tagen ab heute (Standard: 30)"
                }
            },
            "required": []
        }
    }
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_tz(config) -> zoneinfo.ZoneInfo:
    tz_name = getattr(config, 'calendar_timezone', None) or 'Europe/Berlin'
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return zoneinfo.ZoneInfo('Europe/Berlin')


def _parse_start_dt(start_datetime: str, tz: zoneinfo.ZoneInfo) -> datetime:
    """Parse YYYY-MM-DDTHH:MM[...] → UTC datetime."""
    raw = start_datetime.strip()
    # Strip timezone info if present — we treat it as local tz
    raw = re.sub(r'[+-]\d{2}:\d{2}$', '', raw).rstrip('Z')
    # Allow HH:MM or HH:MM:SS
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
        try:
            local_dt = datetime.strptime(raw, fmt).replace(tzinfo=tz)
            return local_dt.astimezone(zoneinfo.ZoneInfo('UTC')).replace(tzinfo=None)
        except ValueError:
            continue
    raise ValueError(f"Ungültiges Datumsformat: '{start_datetime}'. Bitte YYYY-MM-DDTHH:MM verwenden.")


def _notify_owner(instance, config, message: str):
    """Notify the owner on the instance's channel (WhatsApp or Telegram)."""
    from app.services import messaging
    if not messaging.notify_owner(instance, config, message):
        logger.info(f"[Calendar] No owner target configured for instance {instance.id}")


def _fmt_dt(utc_dt: datetime, tz: zoneinfo.ZoneInfo) -> str:
    local = utc_dt.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
    return local.strftime('%d.%m.%Y %H:%M')


# ── Tool implementations ──────────────────────────────────────────────────────

def _check_slots(tool_input: dict, instance, config) -> str:
    from app import db
    from app.models import Appointment

    date_str = tool_input.get('date', '')
    if not _DATE_RE.match(date_str):
        return "Ungültiges Datum. Bitte Format YYYY-MM-DD verwenden."

    tz = _get_tz(config)
    start_h = getattr(config, 'business_hours_start', 9)
    end_h   = getattr(config, 'business_hours_end', 18)
    duration = getattr(config, 'appointment_duration', 60)

    # Parse the target day boundaries in UTC
    day_start_local = datetime.strptime(date_str, '%Y-%m-%d').replace(
        hour=start_h, minute=0, second=0, tzinfo=tz
    )
    day_end_local = datetime.strptime(date_str, '%Y-%m-%d').replace(
        hour=end_h, minute=0, second=0, tzinfo=tz
    )
    day_start_utc = day_start_local.astimezone(zoneinfo.ZoneInfo('UTC')).replace(tzinfo=None)
    day_end_utc   = day_end_local.astimezone(zoneinfo.ZoneInfo('UTC')).replace(tzinfo=None)

    # Check if day is in the past
    now_utc = datetime.utcnow()
    if day_end_utc < now_utc:
        return f"Der {date_str} liegt in der Vergangenheit."

    # Fetch confirmed appointments for this day
    booked = Appointment.query.filter(
        Appointment.instance_id == instance.id,
        Appointment.status == 'confirmed',
        Appointment.start_dt >= day_start_utc,
        Appointment.start_dt < day_end_utc,
    ).all()

    booked_ranges = [(a.start_dt, a.end_dt) for a in booked]

    # Generate slots
    free_slots = []
    cursor = day_start_utc
    while cursor + timedelta(minutes=duration) <= day_end_utc:
        slot_end = cursor + timedelta(minutes=duration)
        # Skip slots in the past (add 15 min buffer for same-day bookings)
        if slot_end <= now_utc + timedelta(minutes=15):
            cursor = slot_end
            continue
        # Check overlap
        overlaps = any(
            not (slot_end <= bs or cursor >= be)
            for bs, be in booked_ranges
        )
        if not overlaps:
            local_cursor = cursor.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
            free_slots.append(local_cursor.strftime('%H:%M'))
        cursor = slot_end

    if not free_slots:
        return (
            f"Am {date_str} sind leider keine freien Termine verfügbar "
            f"(Geschäftszeiten: {start_h:02d}:00–{end_h:02d}:00 Uhr, "
            f"Termindauer: {duration} Min)."
        )

    weekday_de = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    day_name = weekday_de[date_obj.weekday()]
    return (
        f"Freie Termine am {day_name}, {date_obj.strftime('%d.%m.%Y')}: "
        f"{', '.join(free_slots)} Uhr. "
        f"Welcher Termin passt dem Kunden?"
    )


def _create_appointment(tool_input: dict, instance, config, customer_phone: str) -> str:
    from app import db
    from app.models import Appointment

    customer_name  = str(tool_input.get('customer_name', '')).strip()[:200]
    title          = str(tool_input.get('title', '')).strip()[:200]
    start_datetime = str(tool_input.get('start_datetime', '')).strip()
    note           = str(tool_input.get('note', '')).strip()[:500] or None

    if not customer_name or not title or not start_datetime:
        return "Fehler: customer_name, title und start_datetime sind erforderlich."

    tz = _get_tz(config)
    duration = getattr(config, 'appointment_duration', 60)

    try:
        start_utc = _parse_start_dt(start_datetime, tz)
    except ValueError as e:
        return str(e)

    end_utc = start_utc + timedelta(minutes=duration)

    # Validate business hours
    start_local = start_utc.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
    start_h = getattr(config, 'business_hours_start', 9)
    end_h   = getattr(config, 'business_hours_end', 18)
    if not (start_h <= start_local.hour < end_h):
        return (
            f"Der Termin um {start_local.strftime('%H:%M')} liegt außerhalb der "
            f"Geschäftszeiten ({start_h:02d}:00–{end_h:02d}:00 Uhr)."
        )

    # Check for past date
    if start_utc < datetime.utcnow():
        return "Dieser Termin liegt in der Vergangenheit. Bitte wähle ein zukünftiges Datum."

    # Check overlap
    conflict = Appointment.query.filter(
        Appointment.instance_id == instance.id,
        Appointment.status == 'confirmed',
        Appointment.start_dt < end_utc,
        Appointment.end_dt > start_utc,
    ).first()
    if conflict:
        conflict_local = conflict.start_dt.replace(tzinfo=zoneinfo.ZoneInfo('UTC')).astimezone(tz)
        return (
            f"Der Slot {start_local.strftime('%d.%m.%Y %H:%M')} ist bereits belegt "
            f"(Termin '{conflict.title}' um {conflict_local.strftime('%H:%M')} Uhr). "
            "Bitte schlage einen anderen Termin vor."
        )

    appt = Appointment(
        instance_id=instance.id,
        customer_phone=customer_phone,
        customer_name=customer_name,
        title=title,
        start_dt=start_utc,
        end_dt=end_utc,
        note=note,
        status='confirmed',
    )
    db.session.add(appt)
    db.session.commit()

    dt_str = _fmt_dt(start_utc, tz)
    logger.info(f"[Calendar] Appointment #{appt.id} created: {title} at {dt_str} for {customer_phone}")

    # Notify owner
    notif_lines = [
        "📅 *Neuer Termin gebucht!*",
        "",
        f"Kunde: {customer_name}",
        f"Tel: {customer_phone}",
        f"Termin: {title}",
        f"Zeit: {dt_str} Uhr",
    ]
    if note:
        notif_lines.append(f"Notiz: {note}")
    notif_lines.append(f"Buchungs-ID: #{appt.id}")
    _notify_owner(instance, config, '\n'.join(notif_lines))

    return (
        f"Termin erfolgreich gebucht! "
        f"{title} am {dt_str} Uhr für {customer_name} (Buchungs-ID: #{appt.id})."
    )


def _cancel_appointment(tool_input: dict, instance, config, customer_phone: str) -> str:
    from app import db
    from app.models import Appointment

    appt_id = tool_input.get('appointment_id')
    reason  = str(tool_input.get('reason', '')).strip()[:300] or None

    if not isinstance(appt_id, int):
        return "Fehler: appointment_id muss eine Ganzzahl sein."

    appt = Appointment.query.filter_by(id=appt_id, instance_id=instance.id).first()
    if not appt:
        return f"Termin #{appt_id} nicht gefunden."
    if appt.status == 'cancelled':
        return f"Termin #{appt_id} ist bereits storniert."

    tz = _get_tz(config)
    dt_str = _fmt_dt(appt.start_dt, tz)

    appt.status = 'cancelled'
    db.session.commit()

    logger.info(f"[Calendar] Appointment #{appt_id} cancelled by customer {customer_phone}")

    # Notify owner
    notif_lines = [
        "❌ *Termin storniert!*",
        "",
        f"Kunde: {appt.customer_name or customer_phone}",
        f"Tel: {customer_phone}",
        f"Termin: {appt.title}",
        f"Zeit: {dt_str} Uhr",
    ]
    if reason:
        notif_lines.append(f"Grund: {reason}")
    notif_lines.append(f"Buchungs-ID: #{appt_id}")
    _notify_owner(instance, config, '\n'.join(notif_lines))

    return f"Termin '{appt.title}' am {dt_str} Uhr wurde erfolgreich storniert."


def _list_upcoming(customer_phone: str, days: int, instance, config) -> str:
    from app.models import Appointment

    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 30

    now_utc = datetime.utcnow()
    until_utc = now_utc + timedelta(days=days)
    tz = _get_tz(config)

    appts = Appointment.query.filter(
        Appointment.instance_id == instance.id,
        Appointment.customer_phone == customer_phone,
        Appointment.status == 'confirmed',
        Appointment.start_dt >= now_utc,
        Appointment.start_dt <= until_utc,
    ).order_by(Appointment.start_dt).all()

    if not appts:
        return f"Keine bevorstehenden Termine für diese Nummer in den nächsten {days} Tagen."

    lines = [f"Bevorstehende Termine ({len(appts)}):"]
    for a in appts:
        dt_str = _fmt_dt(a.start_dt, tz)
        lines.append(f"• #{a.id} — {a.title} am {dt_str} Uhr")
    return '\n'.join(lines)


# ── Public dispatcher ─────────────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict, instance, config, customer_phone: str) -> str:
    """Dispatch a calendar tool call from the AI."""
    try:
        if tool_name == 'calendar_check_slots':
            return _check_slots(tool_input, instance, config)
        elif tool_name == 'calendar_create_appointment':
            return _create_appointment(tool_input, instance, config, customer_phone)
        elif tool_name == 'calendar_cancel_appointment':
            return _cancel_appointment(tool_input, instance, config, customer_phone)
        elif tool_name == 'calendar_list_upcoming':
            phone = tool_input.get('customer_phone') or customer_phone
            days  = tool_input.get('days', 30)
            return _list_upcoming(phone, days, instance, config)
        else:
            return f"Unbekanntes Calendar-Tool: {tool_name}"
    except Exception as e:
        logger.error(f"[Calendar] execute_tool {tool_name} error: {e}", exc_info=True)
        return f"Kalender-Fehler: {str(e)}"
