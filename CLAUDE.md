# WhatsApp Shopping Assistant — Phase 1 (College Minor Project)

## Context
2-person team, 7-day sprint. Dev A owns FastAPI/webhooks/WhatsApp/Razorpay.
Dev B owns Postgres/ChromaDB/OpenAI RAG pipeline. Deadline: [your date].

## Stack
- FastAPI (Python, async), SQLAlchemy + PostgreSQL
- ChromaDB (persistent client) + OpenAI text-embedding-3-small
- GPT-4o-mini for response generation
- WhatsApp Cloud API v21.0, Razorpay (test mode)

## Repo layout
/backend  - FastAPI app, routers, Postgres models
/rag      - embedding scripts, ChromaDB retrieval, prompt construction
/common   - shared Pydantic schemas (ProductMatch, etc.)

Backend runs from inside /backend: `uvicorn main:app --reload --port 8000`.
Env is loaded from backend/.env by main.py (python-dotenv).

## Shared contract (do not change without syncing both devs)
get_product_recommendations(query: str, top_k: int = 3) -> tuple[str, List[ProductMatch]]

class ProductMatch(BaseModel):
    product_id: str
    name: str
    price_inr: float
    rich_description: str

## Conventions
- All secrets via os.environ, never hardcoded. .env is gitignored.
- Read env vars inside functions, not at import time (dotenv loads in main.py).
- FastAPI routes are async. Outbound HTTP uses httpx.AsyncClient.
- POST /webhook must always return 200, even on parse errors, or Meta retries.
- Env vars: WHATSAPP_ACCESS_TOKEN, WHATSAPP_VERIFY_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
  OPENAI_API_KEY, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, DATABASE_URL

## WhatsApp test-number gotchas
- Test number can only message recipients added in the dashboard "To" list.
- Free-form text only delivers within 24h of the user's last inbound message.
- Dashboard temporary access token expires after ~24h.
- /dev/send-test in main.py is a dev-only route; remove before deploying.

## Status
- [x] Backend scaffold (main.py, routers/webhook.py, whatsapp.py)
- [x] Webhook verification (GET) passed in Meta dashboard
- [x] POST /webhook receiving and processing incoming messages
- [x] send_whatsapp_message() confirmed delivering to a real phone
- [x] Docker Compose (Postgres + ChromaDB)
- [ ] ChromaDB populated with catalog (run `python scripts/ingest_catalog.py`)
      BLOCKED: embedding provider undecided (OpenAI key vs free local MiniLM)
- [x] get_product_recommendations() implemented
- [x] Postgres models + CRUD (models.py, db.py, repository.py)
- [ ] Webhook wired to persistence (users/sessions not written on message)
- [ ] Razorpay order creation + Pay Now interactive message
- [ ] Razorpay payment.captured webhook

## Known gaps
- Contract drift: CLAUDE.md specifies List[ProductMatch] with product_id/price_inr;
  bot/chat.py returns dicts with id/price. /common package does not exist yet.
- RAG code lives in backend/bot/chat.py, not /rag as this doc states.

## Project history
- Meta WhatsApp Cloud API webhook handshake, subscription, inbound message handling,
    and outbound Graph API messaging completed.
- Added `scripts/simulate_webhook.py` for local conversational testing without ngrok
    or a physical phone.
- Added Docker Compose services for PostgreSQL and ChromaDB with persistent volumes.
- Added a 12-item ethnic wear catalog, OpenAI embedding ingestion, Chroma retrieval,
    and Phase 1 webhook-to-RAG response routing.
