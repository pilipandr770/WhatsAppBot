"""
Google Calendar & Sheets service for WhatsApp bot tool use.

Provides:
  - get_credentials(instance_id)      – returns valid Credentials or None
  - execute_tool(name, input, inst_id) – dispatcher for Claude tool calls
  - GOOGLE_TOOLS                       – Anthropic tool definitions list
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
        "name": "google_calendar_create_event",
        "description": (
            "Erstellt einen neuen Termin im Google Kalender des Nutzers. "
            "Nutze dieses Tool, wenn jemand einen Termin, eine Besprechung oder einen Eintrag vereinbaren möchte. "
            "Gib Start- und Endzeit im ISO-8601-Format an (z. B. 2026-05-15T14:00:00+02:00)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Titel des Termins"
                },
                "start_datetime": {
                    "type": "string",
                    "description": "Startzeit im ISO-8601-Format, z. B. 2026-05-20T10:00:00+02:00"
                },
                "end_datetime": {
                    "type": "string",
                    "description": "Endzeit im ISO-8601-Format, z. B. 2026-05-20T11:00:00+02:00"
                },
                "description": {
                    "type": "string",
                    "description": "Optionale Beschreibung des Termins"
                },
                "attendee_email": {
                    "type": "string",
                    "description": "E-Mail-Adresse eines einzuladenden Teilnehmers (optional)"
                }
            },
            "required": ["summary", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "google_calendar_list_events",
        "description": (
            "Listet bevorstehende Termine aus dem Google Kalender auf. "
            "Nutze dieses Tool um freie Termine zu prüfen oder geplante Ereignisse zu nennen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximale Anzahl zurückzugebender Termine (Standard: 5)"
                },
                "days_ahead": {
                    "type": "integer",
                    "description": "Wie viele Tage in die Zukunft schauen (Standard: 7)"
                }
            }
        }
    },
    {
        "name": "google_sheets_read",
        "description": (
            "Liest Daten aus einer Google-Tabelle (Spreadsheet). "
            "Nutze dieses Tool um Informationen wie Preislisten, Produkte, Kundendaten o. ä. nachzuschlagen. "
            "Die spreadsheet_id steht in der URL der Tabelle: docs.google.com/spreadsheets/d/<ID>/edit"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Die ID des Google Spreadsheets"
                },
                "range": {
                    "type": "string",
                    "description": "Der Zellbereich, z. B. 'Tabelle1!A1:D20' oder 'A1:Z100'"
                }
            },
            "required": ["spreadsheet_id", "range"]
        }
    },
    {
        "name": "google_sheets_append",
        "description": (
            "Fügt eine neue Zeile zu einer Google-Tabelle hinzu. "
            "Nutze dieses Tool um Leads, Kontaktanfragen, Bestellungen oder Terminbuchungen zu speichern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Die ID des Google Spreadsheets"
                },
                "range": {
                    "type": "string",
                    "description": "Tabellenbereich zum Anfügen, z. B. 'Tabelle1!A:E'"
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Werte für die neue Zeile (eine Zelle pro Eintrag)"
                }
            },
            "required": ["spreadsheet_id", "range", "values"]
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
        # Set expiry so the library can check if a refresh is needed
        if token_row.token_expiry:
            creds.expiry = token_row.token_expiry.replace(tzinfo=timezone.utc)

        # Refresh if expired or about to expire (within 60 s)
        if creds.expired or (
            creds.expiry and
            creds.expiry - datetime.now(timezone.utc) < timedelta(seconds=60)
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
                logger.warning(f"Google token expired and no refresh_token for instance {instance_id}")
                return None

        return creds

    except Exception as e:
        logger.error(f"get_credentials instance={instance_id}: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Calendar functions
# ---------------------------------------------------------------------------

def create_calendar_event(
    creds,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = '',
    attendee_email: str = ''
) -> str:
    from googleapiclient.discovery import build

    service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    event_body = {
        'summary': summary,
        'start': {'dateTime': start_datetime},
        'end': {'dateTime': end_datetime},
    }
    if description:
        event_body['description'] = description
    if attendee_email:
        event_body['attendees'] = [{'email': attendee_email}]

    created = service.events().insert(calendarId='primary', body=event_body).execute()
    link = created.get('htmlLink', '')
    return (
        f"✅ Termin erstellt: \"{summary}\"\n"
        f"Start: {start_datetime}\nEnde: {end_datetime}\n"
        f"Link: {link}"
    )


def list_calendar_events(creds, max_results: int = 5, days_ahead: int = 7) -> str:
    from googleapiclient.discovery import build

    service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)

    result = service.events().list(
        calendarId='primary',
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        maxResults=max(1, min(int(max_results), 20)),
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    items = result.get('items', [])
    if not items:
        return f"Keine Termine in den nächsten {days_ahead} Tagen gefunden."

    lines = [f"📅 Termine (nächste {days_ahead} Tage):"]
    for ev in items:
        start = ev['start'].get('dateTime', ev['start'].get('date', '?'))
        lines.append(f"• {ev.get('summary', '(kein Titel)')} — {start}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Sheets functions
# ---------------------------------------------------------------------------

def read_sheet(creds, spreadsheet_id: str, range: str) -> str:
    from googleapiclient.discovery import build

    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range
    ).execute()

    rows = result.get('values', [])
    if not rows:
        return "Keine Daten in diesem Bereich gefunden."

    lines = []
    for row in rows[:50]:  # limit output
        lines.append(' | '.join(str(cell) for cell in row))
    return '\n'.join(lines)


def append_to_sheet(creds, spreadsheet_id: str, range: str, values: list) -> str:
    from googleapiclient.discovery import build

    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    body = {'values': [values]}
    result = service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range,
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()

    updates = result.get('updates', {})
    updated_range = updates.get('updatedRange', range)
    return f"✅ Zeile hinzugefügt in: {updated_range}"


# ---------------------------------------------------------------------------
# Tool dispatcher (called from Claude tool-use loop)
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict, instance_id: int) -> str:
    """Execute a Google tool call and return a human-readable result string."""
    creds = get_credentials(instance_id)
    if not creds:
        return (
            "Google ist für diese Instanz nicht verbunden. "
            "Bitte verbinde Google im Dashboard unter Konfiguration → Google-Integration."
        )

    try:
        if tool_name == 'google_calendar_create_event':
            return create_calendar_event(creds, **tool_input)
        elif tool_name == 'google_calendar_list_events':
            return list_calendar_events(creds, **tool_input)
        elif tool_name == 'google_sheets_read':
            return read_sheet(creds, **tool_input)
        elif tool_name == 'google_sheets_append':
            return append_to_sheet(creds, **tool_input)
        else:
            return f"Unbekanntes Tool: {tool_name}"
    except Exception as e:
        logger.error(f"execute_tool {tool_name} instance={instance_id}: {e}", exc_info=True)
        return f"Fehler bei {tool_name}: {str(e)}"
