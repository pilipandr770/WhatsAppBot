# WA.AI — WhatsApp KI-Support SaaS

WhatsApp AI-Support platform. Tenants connect their WhatsApp number,
configure a bot prompt, upload documents (RAG), and Claude answers
customer messages automatically.

## Stack

- **Flask** + PostgreSQL + Redis
- **Evolution API** (WhatsApp Web wrapper, Docker)
- **Anthropic Claude** (AI responses)
- **Celery** (async document processing)
- **Stripe** (subscriptions)
- **Nginx** + Let's Encrypt

---

## Deploy on Hetzner VPS

### 1. Prerequisites

```bash
# On Hetzner Ubuntu 22.04
apt update && apt install -y docker.io docker-compose certbot python3-certbot-nginx git
```

### 2. Clone & configure

```bash
git clone <your-repo> /opt/waai
cd /opt/waai
cp .env.example .env
nano .env   # Fill all values
```

### 3. SSL certificate

```bash
certbot certonly --standalone -d yourdomain.com
# Update nginx.conf with your domain
```

### 4. Launch

```bash
docker-compose up -d --build
docker-compose logs -f web
```

### 5. Stripe Webhook

In Stripe Dashboard → Webhooks → Add endpoint:
- URL: `https://yourdomain.com/billing/webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`

Copy the webhook signing secret → `STRIPE_WEBHOOK_SECRET` in `.env`

### 6. Stripe Prices

Create 3 recurring prices in Stripe Dashboard:
- Starter: €29/month → copy ID → `STRIPE_PRICE_STARTER`
- Pro: €79/month → `STRIPE_PRICE_PRO`
- Business: €199/month → `STRIPE_PRICE_BUSINESS`

---

## Architecture

```
User WhatsApp
     ↓ (message)
Evolution API → POST /wh/{instance_name}
     ↓
Flask webhook handler
     ├── Load BotConfig (system prompt)
     ├── Load conversation history
     ├── RAG search (if documents uploaded)
     └── Claude API → reply
          ↓
Evolution API → send reply to WhatsApp
```

## Multi-tenancy

Each customer = one `WhatsAppInstance` with a unique `instance_name`.
Evolution API supports multiple instances on one container.
Webhook URL per instance: `https://yourdomain.com/wh/{instance_name}`

## Document Processing (RAG)

1. User uploads PDF/DOCX/TXT
2. Celery worker extracts text → chunks into ~400 word pieces
3. Chunks stored in PostgreSQL (`document_chunks` table)
4. On each incoming message: keyword search over chunks → top 3 injected into Claude prompt

Upgrade path: Replace keyword search with pgvector + embeddings for semantic search.

---

## Plans

| Plan     | Price   | Instances |
|----------|---------|-----------|
| Starter  | €29/mo  | 1         |
| Pro      | €79/mo  | 5         |
| Business | €199/mo | 20        |

---

## Useful commands

```bash
# View logs
docker-compose logs -f web worker

# Restart
docker-compose restart web

# DB shell
docker-compose exec postgres psql -U wauser -d whatsapp_saas

# Celery status
docker-compose exec worker celery -A app.tasks.celery inspect active
```
