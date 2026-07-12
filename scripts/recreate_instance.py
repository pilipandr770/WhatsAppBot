"""Recreate an Evolution API instance for an existing DB record.

This scripts the manual recovery procedure for the "QR code never shows"
failure mode: the DB has an instance row, but Evolution API lost the
instance (VPS reinstall, Evolution data wipe, etc.). It recreates the
instance in Evolution KEEPING the token stored in the DB, re-registers
the webhook and stores the fresh QR in the DB so the dashboard shows it
immediately.

Normally the dashboard's "Neu verbinden" button does this via the web app.
Use this script when the web app itself can't do it (e.g. for diagnostics
or bulk recovery after a VPS migration).

Usage:
    python scripts/recreate_instance.py <instance_db_id>

Required environment variables:
    DATABASE_URL              postgresql://... (external Render URL)
    DB_SCHEMA                 e.g. whatsapp_saas (optional)
    EVOLUTION_API_URL         e.g. http://31.97.154.136:32768
    EVOLUTION_API_KEY         Evolution global apikey
    APP_BASE_URL              e.g. https://whatsappbothelfer.de
    EVOLUTION_WEBHOOK_TOKEN   webhook apikey query param (optional)
"""

import os
import sys
import time
from datetime import datetime

import psycopg2
import requests


def env(name, required=True, default=''):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing env var: {name}")
    return val


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    instance_id = int(sys.argv[1])

    db_url = env('DATABASE_URL')
    schema = env('DB_SCHEMA', required=False)
    evo_url = env('EVOLUTION_API_URL').rstrip('/')
    evo_key = env('EVOLUTION_API_KEY')
    app_url = env('APP_BASE_URL').rstrip('/')
    wh_token = env('EVOLUTION_WEBHOOK_TOKEN', required=False)

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    if schema:
        cur.execute(f'SET search_path TO "{schema}"')

    cur.execute(
        "SELECT instance_name, api_token FROM whatsapp_instances WHERE id = %s",
        (instance_id,)
    )
    row = cur.fetchone()
    if not row:
        sys.exit(f"Instance id={instance_id} not found in DB")
    name, token = row
    print(f"Instance: {name} (id {instance_id})")

    headers = {'Content-Type': 'application/json', 'apikey': evo_key}

    # 1. Delete leftover instance (ignore errors — usually already gone)
    r = requests.delete(f'{evo_url}/instance/delete/{name}', headers=headers, timeout=15)
    print(f"delete: HTTP {r.status_code}")
    time.sleep(3)

    # 2. Create with the SAME token the DB already stores
    webhook_url = f"{app_url}/wh/{name}"
    if wh_token:
        webhook_url += f"?apikey={wh_token}"

    payload = {
        'instanceName': name,
        'token': token,
        'qrcode': True,
        'integration': 'WHATSAPP-BAILEYS',
        'webhook': {
            'enabled': True,
            'url': webhook_url,
            'byEvents': False,
            'base64': True,
            'events': ['MESSAGES_UPSERT', 'CONNECTION_UPDATE', 'QRCODE_UPDATED'],
        },
    }
    r = requests.post(f'{evo_url}/instance/create', json=payload, headers=headers, timeout=45)
    print(f"create: HTTP {r.status_code}")
    r.raise_for_status()
    data = r.json()

    qr = (data.get('qrcode') or {}).get('base64') or data.get('base64') or ''
    print(f"QR in response: {'yes' if qr else 'no (will arrive via webhook)'}")

    # 3. Update DB: connecting + fresh QR so the dashboard shows it right away
    cur.execute(
        """UPDATE whatsapp_instances
           SET status = 'connecting', qr_code = %s, qr_updated_at = %s
           WHERE id = %s""",
        (qr or None, datetime.utcnow(), instance_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    print("Done. Open the dashboard connect page — the QR should be visible.")


if __name__ == '__main__':
    main()
