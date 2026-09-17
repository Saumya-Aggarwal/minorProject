# WhatsApp Shopping Assistant — Phase 1 (College Minor Project)

## Context
2-person team, 7-day sprint. Dev A owns FastAPI/webhooks/WhatsApp/Razorpay.
Dev B owns Postgres/ChromaDB/OpenAI RAG pipeline. Deadline: 23 September 2026.

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

AGREED ADDITION (Dev B to implement, backward compatible):
    get_product_recommendations(query, top_k=3, user_context: str | None = None)
user_context carries the linked customer's name and purchase history for the
prompt. Until it exists, webhook.py's _recommend() inspects the signature and
omits the argument, so the current two-parameter version keeps working.

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
- [x] Webhook wired to persistence (user upsert + session turn recording)
- [x] Storefront (product grid, auth, account page) + JSON API under /api
- [x] WhatsApp account linking via single-use token + wa.me deep link
- [x] Bot personalizes replies for linked users (order history in prompt)
- [ ] Multi-turn follow-ups ("the second one") resolved from session context
- [ ] Razorpay order creation + Pay Now interactive message
- [ ] Razorpay payment.captured webhook

## Account linking
A browser session cannot reach the bot: Meta's webhook delivers only a phone
number and a message body. So /account mints a single-use code, the wa.me deep
link pre-fills it, and the webhook binds wa_id to the account on receipt.

The merge matters: a customer who messaged the bot first already has a
bot-created users row holding that number. consume_link_token() re-points its
orders and sessions to the website account and deletes it, in one transaction.
Deleting (not blanking the number) is required — a row with neither email nor
whatsapp_number violates ck_users_has_identity.

Verify with: backend/.venv/Scripts/python scripts/test_linking.py  (18 checks)

## Schema layers (three different things)
1. Postgres tables  - backend/models.py, real types and constraints
2. Pydantic contract - common/schemas.py, ProductMatch, validated in memory
3. Chroma metadata   - untyped dicts in scripts/ingest_catalog.py, NOT validated

ProductMatch.from_raw() accepts both the Chroma key style (id/price/description)
and the contract style (product_id/price_inr/rich_description). The webhook
normalizes through it before storing, so neither side has to change first.

Products are deliberately NOT in Postgres. orders copies product_name and
price_inr at purchase time so an order records what was actually paid.

## Known gaps
- RAG code lives in backend/bot/chat.py, not /rag as this doc states.
- No Alembic. Schema changes mean dropping and recreating tables. Fine while
  the data is disposable; add migrations before this holds anything real.
- /buy is a placeholder that writes an order row directly. Razorpay replaces it.
- ingest_catalog.py and bot/chat.py still use the Chroma key style directly;
  adopting ProductMatch there would remove the last of the drift.

## Project history
- Meta WhatsApp Cloud API webhook handshake, subscription, inbound message handling,
    and outbound Graph API messaging completed.
- Added `scripts/simulate_webhook.py` for local conversational testing without ngrok
    or a physical phone.
- Added Docker Compose services for PostgreSQL and ChromaDB with persistent volumes.
- Added a 12-item ethnic wear catalog, OpenAI embedding ingestion, Chroma retrieval,
    and Phase 1 webhook-to-RAG response routing.
