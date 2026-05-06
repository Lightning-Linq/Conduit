# Conduit

Lightning Payment Rails for AI Agents.

## Quick Start

```bash
# Clone and set up
cp .env.example .env
# Edit .env with your LND node details

# Run with Docker
docker compose up -d

# Or run locally
pip install -e ".[dev]"
uvicorn conduit.main:app --reload

# Run migrations
alembic upgrade head
```

## API Endpoints

- `GET /health` — Health check
- `POST /api/v1/wallets/` — Create agent wallet
- `GET /api/v1/wallets/{id}` — Get wallet
- `GET /api/v1/wallets/{id}/balance` — Get balance
- `POST /api/v1/invoices/` — Create Lightning invoice
- `GET /api/v1/invoices/{id}` — Get invoice
- `POST /api/v1/payments/` — Send Lightning payment
- `GET /api/v1/payments/{id}` — Get payment

## Architecture

```
src/conduit/
├── api/routers/     # FastAPI endpoint handlers
├── core/            # Config, database, shared utilities
├── models/          # SQLAlchemy ORM models
├── schemas/         # Pydantic request/response schemas
└── services/        # Business logic (LND client, etc.)
```

## LND Setup

Point `LND_TLS_CERT_PATH` and `LND_MACAROON_PATH` in your `.env` to your node credentials. Run `scripts/gen_protos.sh` to compile the gRPC stubs.
