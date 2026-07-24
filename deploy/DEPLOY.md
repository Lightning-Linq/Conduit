# Deploying the hosted Conduit API

This runs the full Conduit backend (REST API + Postgres + Redis) behind Caddy
with automatic HTTPS, on a single small VPS. It is separate from the seed-skills
webhook box (`seed-skills/DEPLOY.md`); keep the money-handling API on its own host.

Stack: `docker-compose.prod.yml` (API, Postgres, Redis, Caddy). Only Caddy is
exposed to the internet (80/443); the database and API stay on the internal
network. Secrets live in `.env`, which is never committed.

## 1. Host + DNS
- Ubuntu 24.04 droplet, 2 GB RAM (spends sats and holds the DB; keep it separate
  from public keyless endpoints).
- DNS A record: `api.lightninglinq.ai` -> the droplet's public IP.
- Firewall: allow 22, 80, 443 only.

## 2. Install Docker + clone
```bash
curl -fsSL https://get.docker.com | sh
cd /root && git clone https://github.com/Lightning-Linq/Conduit.git && cd Conduit
```

## 3. Configure `.env`
```bash
cp .env.example .env
# then edit .env — the values that MUST change for production:
```
- `APP_ENV=production`
- `CONDUIT_API_KEY` — a strong random key: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `POSTGRES_PASSWORD` — a strong random value (the prod compose reads it).
- `DATABASE_URL=postgresql+asyncpg://conduit:<that-password>@postgres:5432/conduit`
  (host is the compose service name `postgres`, and the password must match
  `POSTGRES_PASSWORD`).
- `REDIS_URL=redis://redis:6379/0` (host is the compose service name `redis`).
- `WALLET_BACKEND=nwc` and `NWC_CONNECTION_STRING=...` — your wallet's NWC string
  (SECRET; paste it directly into `.env`, never anywhere else).
- `CORS_ALLOW_ORIGINS=https://lightninglinq.ai`

Optional / later: `L402_ENABLED=true` + `L402_SECRET_KEY` (public pay-per-use
auth), `NOSTR_PRIVATE_KEY` (node identity), `PLATFORM_FEE_NWC_URI` +
`SUBSCRIPTION_ENABLED=true` (turn on seller subscriptions).

## 4. Launch
```bash
docker compose -f docker-compose.prod.yml up -d --build
```
The API entrypoint runs `alembic upgrade head` automatically. Caddy obtains a
Let's Encrypt cert for `api.lightninglinq.ai` (needs the DNS record from step 1
live and ports 80/443 open).

## 5. Verify
```bash
docker compose -f docker-compose.prod.yml ps          # all services Up/healthy
docker compose -f docker-compose.prod.yml logs -f api  # watch startup + migrations
curl https://api.lightninglinq.ai/health               # from anywhere
```

## Updating
```bash
git pull && docker compose -f docker-compose.prod.yml up -d --build
```
