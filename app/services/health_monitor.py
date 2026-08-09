"""Background health monitor: keeps DB instance status in sync with Evolution API.

A daemon thread runs inside every gunicorn worker, but only one worker executes
a tick at a time: a PostgreSQL advisory lock serializes execution and a
SiteConfig timestamp enforces the check interval across workers.

What a tick does:
1. Pings Evolution API (fetchInstances). If unreachable -> admin alert once
   (with cooldown), skip the rest. Sends a recovery alert when it comes back.
2. Instances marked 'connected' in DB but missing in Evolution (the class of
   bug where QR never shows) -> mark disconnected + admin alert.
3. Instances marked 'connected' whose Evolution state is not 'open' two ticks
   in a row -> try trigger_connect (session resume), mark disconnected, alert.
   The two-tick rule avoids flapping alerts during brief reconnects.
4. Reverse sync: DB says disconnected/connecting but Evolution says 'open'
   -> fix DB to 'connected' (covers missed CONNECTION_UPDATE webhooks).

Admin alerts go to ADMIN_ALERT_PHONE via WhatsApp (sent from a connected
instance owned by an admin user) and/or email via SMTP_* env vars.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_ID = 872934018          # distinct from the boot-migration lock
_ALERT_COOLDOWN = 30 * 60              # seconds between identical alerts

_started = False


def start_health_monitor(app):
    """Start the monitor daemon thread (once per process)."""
    global _started
    if _started:
        return
    if os.environ.get('HEALTH_MONITOR_ENABLED', 'true').lower() != 'true':
        logger.info("[Health] monitor disabled via HEALTH_MONITOR_ENABLED")
        return
    _started = True
    interval = int(os.environ.get('HEALTH_CHECK_INTERVAL', '300'))
    t = threading.Thread(target=_loop, args=(app, interval), daemon=True,
                         name='health-monitor')
    t.start()
    logger.info(f"[Health] monitor started (interval {interval}s)")


def _loop(app, interval):
    time.sleep(60)  # let the app finish booting before the first check
    while True:
        try:
            with app.app_context():
                _maybe_tick(app, interval)
        except Exception as e:
            logger.error(f"[Health] tick crashed: {e}", exc_info=True)
        time.sleep(60)


def _maybe_tick(app, interval):
    """Run a tick if the interval elapsed and no other worker is checking."""
    from app import db
    from app.models import SiteConfig

    if db.engine.dialect.name != 'postgresql':
        _tick(app)  # dev/sqlite: no locking needed
        return

    lock_conn = db.engine.connect()
    try:
        got = lock_conn.execute(
            text(f"SELECT pg_try_advisory_lock({_ADVISORY_LOCK_ID})")
        ).scalar()
        if not got:
            return  # another worker is mid-check

        last = SiteConfig.get('health_last_check', '')
        if last:
            try:
                elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                if elapsed < interval:
                    return
            except ValueError:
                pass

        SiteConfig.set('health_last_check', datetime.utcnow().isoformat())
        db.session.commit()
        _tick(app)
    finally:
        try:
            lock_conn.execute(text(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_ID})"))
        finally:
            lock_conn.close()


def _tick(app):
    from app import db
    from app.models import SiteConfig, WhatsAppInstance
    from app.services.evolution import evolution_client

    # ── 1. Evolution API reachability ────────────────────────────────────────
    evo_names = evolution_client.fetch_instance_names()
    prev_state = SiteConfig.get('health_evo_state', 'up')

    if evo_names is None:  # unreachable
        if prev_state == 'up':
            SiteConfig.set('health_evo_state', 'down')
            db.session.commit()
            _notify_admin(
                "🔴 *Evolution API nicht erreichbar!*\n\n"
                "Der WhatsApp-Server (VPS) antwortet nicht. "
                "Alle Instanzen sind betroffen. Bitte VPS prüfen.",
                alert_key='evo_down'
            )
        logger.warning("[Health] Evolution API unreachable — skipping instance checks")
        return

    if prev_state == 'down':
        SiteConfig.set('health_evo_state', 'up')
        db.session.commit()
        _notify_admin("🟢 *Evolution API wieder erreichbar.*", alert_key='evo_up')

    # Names that failed the state check last tick (two-strike rule)
    try:
        pending = set(json.loads(SiteConfig.get('health_pending', '[]')))
    except (ValueError, TypeError):
        pending = set()
    new_pending = set()

    # ── 2+3. WhatsApp instances the DB believes are connected ────────────────
    # Only WhatsApp/Evolution instances belong here. Telegram instances live on
    # the Bot API (webhook-managed) and never appear in Evolution's instance
    # list, so including them would falsely mark them "vanished" every tick.
    connected = WhatsAppInstance.query.filter_by(status='connected', channel='whatsapp').all()
    for inst in connected:
        name = inst.instance_name

        if name not in evo_names:
            # Instance vanished from Evolution — QR flow would silently fail.
            inst.status = 'disconnected'
            db.session.commit()
            logger.error(f"[Health] {name} missing in Evolution — marked disconnected")
            _notify_admin(
                f"⚠️ *Instanz verschwunden!*\n\n"
                f"Instanz: {name} (ID {inst.id})\n"
                f"Die Instanz existiert nicht mehr in Evolution API. "
                f"Der Kunde muss den QR-Code neu scannen "
                f"(Dashboard → Verbinden → Neu verbinden).",
                alert_key=f'vanished_{name}'
            )
            continue

        state = evolution_client.get_connection_state(name, inst.api_token)
        if state == 'open':
            continue

        if name in pending:
            # Second consecutive failed check — act.
            try:
                evolution_client.trigger_connect(name, inst.api_token)
            except Exception:
                pass
            inst.status = 'disconnected'
            db.session.commit()
            logger.error(f"[Health] {name} state={state} twice — marked disconnected")
            _notify_admin(
                f"⚠️ *WhatsApp-Verbindung verloren!*\n\n"
                f"Instanz: {name} (ID {inst.id})\n"
                f"Status: {state}\n"
                f"Automatischer Reconnect wurde versucht. Falls die Verbindung "
                f"nicht zurückkommt, muss der QR-Code neu gescannt werden.",
                alert_key=f'lost_{name}'
            )
        else:
            new_pending.add(name)
            logger.warning(f"[Health] {name} state={state} — will recheck next tick")

    # ── 4. Reverse sync: DB stale, Evolution actually connected ──────────────
    stale = WhatsAppInstance.query.filter(
        WhatsAppInstance.status.in_(('disconnected', 'connecting')),
        WhatsAppInstance.channel == 'whatsapp',
    ).all()
    for inst in stale:
        if inst.instance_name in evo_names and inst.api_token:
            state = evolution_client.get_connection_state(inst.instance_name, inst.api_token)
            if state == 'open':
                inst.status = 'connected'
                inst.qr_code = None
                db.session.commit()
                logger.info(f"[Health] {inst.instance_name} actually open — DB fixed to connected")

    SiteConfig.set('health_pending', json.dumps(sorted(new_pending)))
    db.session.commit()
    logger.info(f"[Health] tick done: {len(connected)} connected checked, "
                f"{len(new_pending)} pending recheck")


# ─── Admin notification ──────────────────────────────────────────────────────

def _notify_admin(message: str, alert_key: str = ''):
    """Send an alert to the platform admin via WhatsApp and/or email.

    alert_key enables a cooldown so the same problem doesn't alert every tick.
    """
    from app import db
    from app.models import SiteConfig

    if alert_key:
        stamp_key = f'health_alert_{alert_key}'
        last = SiteConfig.get(stamp_key, '')
        if last:
            try:
                if (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < _ALERT_COOLDOWN:
                    return
            except ValueError:
                pass
        SiteConfig.set(stamp_key, datetime.utcnow().isoformat())
        db.session.commit()

    logger.error(f"[Health][ALERT] {message}")
    _send_whatsapp_alert(message)
    _send_email_alert(message)


def _send_whatsapp_alert(message: str):
    """Send via a connected instance owned by an admin user."""
    phone = os.environ.get('ADMIN_ALERT_PHONE', '').strip()
    if not phone:
        return
    try:
        from app.models import User, WhatsAppInstance
        from app.services.evolution import evolution_client

        inst = (WhatsAppInstance.query
                .join(User, WhatsAppInstance.user_id == User.id)
                .filter(User.is_admin.is_(True),
                        WhatsAppInstance.status == 'connected')
                .first())
        if not inst:
            logger.warning("[Health] no connected admin instance for WhatsApp alert")
            return
        evolution_client.send_text(inst.instance_name, inst.api_token, phone, message)
        logger.info(f"[Health] WhatsApp alert sent to {phone}")
    except Exception as e:
        logger.error(f"[Health] WhatsApp alert failed: {e}")


def _send_email_alert(message: str):
    """Send via SMTP if configured (SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_EMAIL)."""
    host = os.environ.get('SMTP_HOST', '')
    to_addr = os.environ.get('ALERT_EMAIL', '')
    if not host or not to_addr:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText

        user = os.environ.get('SMTP_USER', '')
        password = os.environ.get('SMTP_PASS', '')
        port = int(os.environ.get('SMTP_PORT', '587'))

        msg = MIMEText(message.replace('*', ''), 'plain', 'utf-8')
        msg['Subject'] = 'WhatsApp KI Helfer — Health Alert'
        msg['From'] = user or f'alerts@{host}'
        msg['To'] = to_addr

        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        logger.info(f"[Health] email alert sent to {to_addr}")
    except Exception as e:
        logger.error(f"[Health] email alert failed: {e}")
