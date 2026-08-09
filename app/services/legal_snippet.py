"""Ready-to-paste legal text block for the CUSTOMER's own website.

The business owner (our customer) has their own legal obligations towards THEIR
end-customers. We can't write their whole privacy policy, but we can pre-fill the
part that describes what OUR platform does on their behalf — the AI assistant,
what it processes, the EU AI Act transparency measures, security, and the
sub-processors involved. The owner pastes this into their own
Datenschutzerklärung and adds their company-specific parts.

The text is personalised from the instance/config (channel, AI provider, voice,
calendar) so it reflects exactly what that bot uses. It is a TEMPLATE, not legal
advice — the UI makes that clear and recommends a review by the owner's advisor.
"""

_PROVIDER_LINES = {
    'anthropic': "Anthropic, Inc. (Claude, USA) — auf Basis von EU-Standardvertragsklauseln",
    'openai':    "OpenAI, LLC (USA) — auf Basis von EU-Standardvertragsklauseln",
    'mistral':   "Mistral AI, SAS (Frankreich, EU) — Verarbeitung innerhalb der EU",
    'local':     "einem selbstgehosteten KI-Modell — keine Übermittlung an externe Dritte",
}


def build_privacy_snippet(user, instance, config) -> str:
    company = (getattr(user, 'company', None) or '').strip() or '[DEIN UNTERNEHMEN]'
    contact = (getattr(user, 'email', None) or '').strip() or '[DEINE KONTAKT-E-MAIL]'
    channel = 'Telegram' if getattr(instance, 'channel', 'whatsapp') == 'telegram' else 'WhatsApp'

    provider = getattr(config, 'ai_provider', None) or 'anthropic'
    provider_line = _PROVIDER_LINES.get(provider, "einem KI-Anbieter")

    has_calendar = bool(getattr(config, 'calendar_enabled', False)) or bool(getattr(instance, 'google_token', None))

    parts = []
    parts.append(f"Einsatz eines KI-Assistenten über {channel}")
    parts.append("")
    parts.append(
        f"Für die Kommunikation über {channel} setzt {company} einen KI-gestützten, "
        f"automatischen Assistenten ein. Nachrichten, die Sie an unseren {channel}-Kanal "
        f"senden, werden automatisiert verarbeitet, um Ihre Anfragen zu beantworten."
    )
    parts.append("")
    parts.append("Transparenz (Art. 50 EU-KI-Verordnung):")
    parts.append(
        "Sie kommunizieren mit einem automatischen KI-Assistenten und nicht mit einem Menschen. "
        "Der Assistent weist zu Beginn eines Gesprächs darauf hin. Auf Wunsch können Sie jederzeit "
        "an einen menschlichen Mitarbeiter weitergeleitet werden."
    )
    parts.append("")
    parts.append("Verarbeitete Daten:")
    parts.append(
        "• Ihre Text- und Sprachnachrichten sowie Ihr Anzeigename/Ihre Kennung im Messenger.\n"
        "• Sprachnachrichten werden zur Beantwortung automatisch in Text umgewandelt "
        "(Transkription); die Audiodatei wird nicht dauerhaft gespeichert.\n"
        "• Der Gesprächsverlauf wird gespeichert, um den Kontext der Unterhaltung bereitzustellen."
    )
    if has_calendar:
        parts.append(
            "• Bei Terminanfragen werden das gewünschte Datum/Uhrzeit sowie ein von Ihnen "
            "angegebener Name verarbeitet, um die Verfügbarkeit zu prüfen und den Termin zu erfassen."
        )
    parts.append("")
    parts.append("Eingesetzte Dienstleister (Auftragsverarbeiter):")
    parts.append(
        f"• Zur Erzeugung der KI-Antworten werden Nachrichteninhalte übermittelt an: {provider_line}.\n"
        "• Der Betrieb des Assistenten und die Datenspeicherung erfolgen über die Plattform "
        "„WhatsApp KI Helfer“ (Andrii Pylypchuk, Frankfurt am Main), mit Servern in Deutschland (Frankfurt)."
    )
    parts.append("")
    parts.append("Sicherheit:")
    parts.append(
        "Alle Übertragungen erfolgen verschlüsselt (HTTPS/TLS). Der Zugriff auf gespeicherte "
        "Daten ist auf autorisierte Systeme beschränkt. Es werden keine besonders sensiblen "
        "Daten (z. B. Gesundheits- oder Zahlungsdaten) und keine Passwörter abgefragt."
    )
    parts.append("")
    parts.append("Rechtsgrundlage:")
    parts.append(
        "Die Verarbeitung erfolgt zur Bearbeitung Ihrer Anfrage (Art. 6 Abs. 1 lit. b DSGVO) "
        "bzw. auf Grundlage unseres berechtigten Interesses an einer effizienten Kundenkommunikation "
        "(Art. 6 Abs. 1 lit. f DSGVO)."
    )
    parts.append("")
    parts.append("Ihre Rechte:")
    parts.append(
        "Sie haben das Recht auf Auskunft, Berichtigung, Löschung, Einschränkung, "
        "Datenübertragbarkeit und Widerspruch. Wenden Sie sich hierzu an: " + contact + "."
    )
    return '\n'.join(parts)
