"""
Google Calendar free/busy service for WhatsApp bot tool use.

Scope used: https://www.googleapis.com/auth/calendar.freebusy
  - Non-sensitive (no Google verification required)
  - Checks availability ONLY — cannot read event details or create events

Provides:
  - get_credentials(instance_id)       – returns valid Credentials or None
  - execute_tool(name, input, inst_id) – dispatcher for Claude tool calls
  - GOOGLE_TOOLS                        – Anthropic tool definitions list
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions for Claude (Anthropic format)
# ---------------------------------------------------------------------------

GOOGLE_TOOLS = [
    {
        "name": "google_calendar_check_slot",
        "description": (
            "Prüft ob ein bestimmtes Zeitfenster im Google Kalender frei oder belegt ist. "
            "Nutze dieses Tool wenn ein Kunde fragt ob ein konkreter Termin möglich ist, "
            "z. B. 'Ist Dienstag um 10 Uhr frei?' "
            "Gib Start- und Endzeit im ISO-8601-Format an (z. B. 2026-05-20T10:00:00+02:00)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_datetime": {
                    "type": "string",
                    "description": "Startzeit im ISO-8601-Format mit Zeitzone, z. B. 2026-05-20T10:00:00+02:00"
                },
                "end_datetime": {
                    "type": "string",
                    "description": "Endzeit im ISO-8601-Format mit Zeitzone, z. B. 2026-05-20T11:00:00+02:00"
                }
            },
            "required": ["start_datetime", "end_datetime"]
        }
    },
    {
        "name": "google_calendar_get_free_slots",
        "description": (
            "Gibt alle freien Zeitfenster für einen bestimmten Tag zurück. "
            "Nutze dieses Tool wenn ein Kunde nach verfügbaren Terminen fragt, "
            "z. B. 'Wann habt ihr nächste Woche Zeit?' oder 'Welche Termine sind am Montag frei?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Datum im Format YYYY-MM-DD, z. B. 2026-05-20"
                },
                "business_start_hour": {
                    "type": "integer",
                    "description": "Beginn der Geschaeftszeiten in Stunden (Standard: 9)"
                },
                "business_end_hour": {
                    "type": "integer",
                    "description": "Ende der Geschaeftszeiten in Stunden (Standard: 17)"
                },
                "slot_duration_minutes": {
                    "type": "integer",
                    "description": "Laenge eines Terminfensters in Minuten (Standard: 60)"
                }
            },
            "required": ["date"]
        }
    }
]


# ---------------------------------------------------------------------------
# Credentials helper
# ---------------------------------------------------------------------------

def get_credentials(instance_id: int):
    """
    Return valid google.oauth2.credentials.Credentials for the given instance,
    auto-refreshing the access token if it has expired.
    Returns None if no token is stored or refresh fails.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from app import db
        from app.models import GoogleToken

        token_row = GoogleToken.query.filter_by(instance_id=instance_id).first()
        if not token_row:
            return None

        scopes = json.loads(token_row.scopes) if token_row.scopes else []

        creds = Credentials(
            token=token_row.access_token,
            refresh_token=token_row.refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ.get('GOOGLE_CLIENT_ID', ''),
            client_secret=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
            scopes=scopes,
        )
        if token_row.token_expiry:
            creds.expiry = token_row.token_expiry

        now_utc = datetime.utcnow()
        if creds.expired or (
            creds.expiry and
            creds.expiry - now_utc < timedelta(seconds=60)
        ):
            if creds.refresh_token:
                creds.refresh(Request())
                token_row.access_token = creds.token
                token_row.token_expiry = (
                    creds.expiry.replace(tzinfo=None) if creds.expiry else None
                )
                db.session.commit()
                logger.info(f"Google token refreshed for instance {instance_id}")
            else:
                logger.warning(f"Google token expired, no refresh_token for instance {instance_id}")
                return None

        return creds

    except Exception as e:
        logger.error(f"get_credentials instance={instance_id}: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Calendar free/busy functions
# ---------------------------------------------------------------------------

def check_calendar_slot(creds, start_datetime: str, end_datetime: str) -> str:
    """Check if a specific time slot is free using the freebusy API."""
    from googleapiclient.discovery import build

    service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    body = {
        "timeMin": start_datetime,
        "timeMax": end_datetime,
        "items": [{"id": "primary"}]
    }
    result = service.freebusy().query(body=body).execute()
    busy = result.get('calendars', {}).get('primary', {}).get('busy', [])

    if not busy:
        return (
            f"Zeitfenster {start_datetime} bis {end_datetime} ist frei. "
            "Der Kunde kann diesen Termin waehlen."
        )
    busy_str = ', '.join(f"{b['start']} bis {b['end']}" for b in busy)
    return (
        f"Das Zeitfenster ist leider belegt: {busy_str}. "
        "Bitte schlage dem Kunden einen anderen Termin vor."
    )


def get_free_slots(
    creds,
    date: str,
    business_start_hour: int = 9,
    business_end_hour: int = 17,
    slot_duration_minutes: int = 60
) -> str:
    """Return free time slots for a given day using the freebusy API."""
    from googleapiclient.discovery import build

    tz = '+02:00'
    time_min = f"{date}T{business_start_hour:02d}:00:00{tz}"
    time_max = f"{date}T{business_end_hour:02d}:00:00{tz}"

    service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": "Europe/Berlin",
        "items": [{"id": "primary"}]
    }
    result = service.freebusy().query(body=body).execute()
    busy = result.get('calendars', {}).get('primary', {}).get('busy', [])

    free_slots = []
    current_min = business_start_hour * 60
    end_min = business_end_hour * 60

    while current_min + slot_duration_minutes <= end_min:
        s_h, s_m = divmod(current_min, 60)
        e_min = current_min + slot_duration_minutes
        e_h, e_m = divmod(e_min, 60)

        slot_start_iso = f"{date}T{s_h:02d}:{s_m:02d}:00{tz}"
        slot_end_iso   = f"{date}T{e_h:02d}:{e_m:02d}:00{tz}"

        is_busy = any(
            not (slot_end_iso <= b['start'] or slot_start_iso >= b['end'])
            for b in busy
        )
        if not is_busy:
            free_slots.append(f"{s_h:02d}:{s_m:02d}-{e_h:02d}:{e_m:02d} Uhr")

        current_min += slot_duration_minutes

    if not free_slots:
        return (
            f"Am {date} sind keine freien Termine verfuegbar "
            f"(Geschaeftszeiten {business_start_hour}-{business_end_hour} Uhr)."
        )

    return (
        f"Freie Termine am {date}: {', '.join(free_slots)}. "
        "Bitte frage den Kunden welches Zeitfenster passt."
    )


# ---------------------------------------------------------------------------
# Tool dispatcher (called from Claude tool-use loop)
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict, instance_id: int) -> str:
    """Execute a Google Calendar freebusy tool call."""
    creds = get_credentials(instance_id)
    if not creds:
        return (
            "Google Kalender ist nicht verbunden. "
            "Bitte verbinde Google im Dashboard unter Konfiguration."
        )

    try:
        if tool_name == 'google_calendar_check_slot':
            return check_calendar_slot(creds, **tool_input)
        elif tool_name == 'google_calendar_get_free_slots':
            return get_free_slots(creds, **tool_input)
        else:
            return f"Unbekanntes Tool: {tool_name}"
    except Exception as e:
        logger.error(f"execute_tool {tool_name} instance={instance_id}: {e}", exc_info=True)
        return f"Fehler bei {tool_name}: {str(e)}"
