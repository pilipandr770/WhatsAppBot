# Ops: мониторинг, бэкапы, восстановление

## 1. Health-монитор (встроенный)

`app/services/health_monitor.py` — фоновый поток внутри веб-процесса, тик каждые 5 минут:

- Пингует Evolution API; при недоступности — алерт админу (WhatsApp + email), при восстановлении — повторный алерт.
- Инстансы со статусом `connected` в БД, но исчезнувшие из Evolution → помечаются `disconnected` + алерт («класс бага QR не показывается»).
- Инстансы, у которых Evolution-состояние не `open` два тика подряд → авто-`trigger_connect` + `disconnected` + алерт.
- Обратная синхронизация: БД говорит `disconnected`, Evolution — `open` → чинится статус в БД.

### Переменные окружения (Render → Environment)

| Переменная | Назначение |
|---|---|
| `HEALTH_MONITOR_ENABLED` | `true` (по умолчанию) / `false` |
| `HEALTH_CHECK_INTERVAL` | секунды между тиками, по умолчанию `300` |
| `ADMIN_ALERT_PHONE` | номер для WhatsApp-алертов, напр. `4917664952672` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL` | email-алерты (опционально) |

WhatsApp-алерты отправляются с первого подключённого инстанса admin-пользователя.
Повторные алерты по одной и той же проблеме — не чаще раза в 30 минут.

## 2. Внешний мониторинг (UptimeRobot — настроить один раз)

1. Зарегистрироваться на https://uptimerobot.com (бесплатный план — 50 мониторов).
2. Добавить два монитора типа HTTP(s), интервал 5 минут:
   - `https://whatsappbothelfer.de/healthz` — жив ли сам веб-сервис.
   - `https://whatsappbothelfer.de/healthz/deep` — проверяет БД **и** Evolution API (VPS); при падении любого возвращает 503 → алерт.
3. В Alert Contacts добавить свой email (и опционально Telegram).

`/healthz/deep` отвечает JSON вида `{"db": "ok", "evolution": "ok"}`.

## 3. Бэкапы БД (GitHub Actions)

`.github/workflows/db-backup.yml` — ежедневно в 02:30 UTC делает `pg_dump`,
шифрует AES-256 и сохраняет как artifact (хранится 30 дней).

### Настройка (один раз)

GitHub repo → Settings → Secrets and variables → Actions → New repository secret:

- `DATABASE_URL` — внешний connection string Render Postgres.
- `BACKUP_PASSPHRASE` — придуманная фраза для шифрования. **Сохранить в надёжном месте — без неё бэкап не расшифровать.**

Первый запуск: вкладка Actions → DB Backup → Run workflow (проверить, что зелёный).

### Восстановление

```bash
# 1. Скачать artifact из Actions → DB Backup → нужный run → Artifacts
# 2. Расшифровать:
openssl enc -d -aes-256-cbc -pbkdf2 -in backup-2026-07-12.dump.enc \
  -out backup.dump -pass pass:<BACKUP_PASSPHRASE>
# 3. Восстановить (ОСТОРОЖНО: --clean удаляет существующие таблицы):
pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" backup.dump
```

## 4. Восстановление Evolution-инстанса (`recreate_instance.py`)

Когда: в БД инстанс есть, а в Evolution пропал (симптом — QR-код не показывается,
`qr_updated_at` не обновляется). Обычно достаточно кнопки **«Neu verbinden»** в
дашборде; скрипт нужен, если веб-приложение само не справляется.

```bash
export DATABASE_URL='postgresql://...'
export DB_SCHEMA='whatsapp_saas'
export EVOLUTION_API_URL='http://<vps-ip>:32768'
export EVOLUTION_API_KEY='<global key>'
export APP_BASE_URL='https://whatsappbothelfer.de'
export EVOLUTION_WEBHOOK_TOKEN='<webhook token>'

python scripts/recreate_instance.py <instance_db_id>
```

Скрипт: удаляет остатки инстанса в Evolution → создаёт заново **с тем же токеном
из БД** → регистрирует webhook → сохраняет свежий QR в БД. После этого клиент
сканирует QR в дашборде.

## 5. Evolution API на VPS (Hostinger, Frankfurt)

- Docker-контейнер, порт 32768 (открыт в UFW).
- При падении: `ssh root@<vps-ip>`, `docker ps -a`, `docker restart <evolution-container>`.
- `/healthz/deep` и health-монитор оба заметят падение автоматически.
