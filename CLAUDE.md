# WhatsApp Shopping Assistant — Phase 1 (College Minor Project)

## Context
2-person team, 7-day sprint. Deadline: 23 September 2026.
Dev A owns BUYING: FastAPI routers, WhatsApp, Razorpay, and the Postgres schema
(models.py, db.py, repository.py). Dev B owns FINDING: ChromaDB, embeddings, the
RAG/LLM pipeline (bot/chat.py), catalog.py, and the browse/search pages.

WORK_SPLIT.md holds the file-ownership table and the contracts (C1-C5) between
the two halves. Do not edit a file the other dev owns without asking them, and
never change a contract without syncing both devs. plan.md has the architecture
for the remaining work.

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
- [x] ChromaDB populated with catalog (32 products, 384 dims, local MiniLM)
- [x] get_product_recommendations() implemented
- [x] Postgres models + CRUD (models.py, db.py, repository.py)
- [x] Webhook wired to persistence (user upsert + session turn recording)
- [x] Storefront (product grid, auth, account page) + JSON API under /api
- [x] WhatsApp account linking via single-use token + wa.me deep link
- [x] Bot personalizes replies for linked users (order history in prompt)
- [x] Multi-turn follow-ups ("2", "the second one") resolved from session context
- [x] BUY places an order from the selected item (placeholder until Razorpay)
- [x] A1: orders split into header + order_items; shared cart_items; C2 get_purchased_product_ids
- [x] A2: cart on WhatsApp (ADD, CART, REMOVE n, SIZE n x, CHECKOUT) and on the web (/cart, /api/cart)
- [x] A3: Razorpay Payment Links for both channels; Pay Now cta_url button in chat;
      confirmation by redirect (verified against Razorpay's API) and by signed webhook
- [x] A3 live: real payment link created and fetched via Razorpay test API
- [x] A4: web orders confirm on the customer's WhatsApp when their account is linked
- [x] A5: /orders/{id} tracking page (placed, paid, arriving by), ORDERS in chat
- [x] Chat greeting and HELP (a bare "hi" no longer runs a product search)
- [x] End-to-end payment on a real phone
- [x] Live sync: website follows WhatsApp changes (cart badge, cart/account/order
      pages re-render in place, toasts) via 2-second polling of /api/live

## Live sync
static/live.js polls GET /api/live every 2s on signed-in pages. The response is
a fingerprint of cart lines + recent order statuses; the page re-renders its
<main> only when that changes, and only on pages whose template sets the
live_attr block (cart, account, order, payment result). It never replaces the
page while a form field inside it has focus. Polling rather than WebSockets:
nothing to reconnect across ngrok, server restarts or bad Wi-Fi.
/api/live reads the user with current_user(touch=False) so polling never writes.
The account page reuses a still-valid WhatsApp link code (get_or_create_link_token)
so a re-render cannot invalidate a code the customer is about to send.
Verify: scripts/test_live.py (server) and node scripts/test_live_js.mjs (browser logic).

## Payments
checkout.py orchestrates; payments.py is a thin httpx wrapper over three Razorpay
endpoints (no SDK: it is synchronous). Payment Links serve both channels.
Two confirmation paths, either sufficient: GET /payments/callback (customer's
browser; confirmed by fetching the link from Razorpay, never by trusting query
params) and POST /razorpay/webhook (event payment_link.paid, HMAC over the RAW
body). mark_order_paid is idempotent and row-locked (SELECT ... FOR UPDATE), so
both paths together send exactly one WhatsApp confirmation.
On payment, only the ordered quantities leave the cart. If Razorpay is down, the
order just created is cancelled. CHECKOUT twice reuses the unpaid order.
Verify with: backend/.venv/Scripts/python scripts/test_payments.py

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

## Embeddings
EMBEDDING_PROVIDER in backend/.env selects the model, used by both ingestion and
retrieval through common/embeddings.py:
  local  (default) all-MiniLM-L6-v2, 384 dims, CPU, no API key, cannot rate-limit
  openai           text-embedding-3-small, 1536 dims, needs OPENAI_API_KEY
Switching provider changes vector width — re-run scripts/ingest_catalog.py after.

Retrieval quality improved markedly with the richer 32-product catalog, because
the embedded text now carries occasion, fabric, colour and fit vocabulary.
Price and stock are deliberately NOT embedded: they are filters, not meaning.

bot/chat.py applies a gender metadata filter when the query names hints from
exactly one set (groom/sister/saree/sherwani...). Without it "groom outfit"
ranked a women's kurta set first — wedding vocabulary drowned out the one word
that mattered. Embeddings alone do not respect hard constraints; metadata
filtering is the reliable fix.

Still imperfect: "jacket to wear over my kurta" ranks a kurta above the Nehru
jacket, and "me and my wife" filters to Women. Both need category filtering or
intent parsing. Dev B's retrieval-tuning task.

## Follow-up handling
backend/selection.py parses a reply against last_products_shown. It requires the
WHOLE message to match a selector pattern, never a substring: "2 piece kurta set"
and "under 3000" contain digits but are searches. Out-of-range numbers are
treated as searches too.

sessions.selected_product holds the chosen item so a later BUY knows what it
means; starting a new search clears it. Razorpay's Pay Now button will read the
same field.

## Catalog
data/products.json — 32 products, men's and women's Indian ethnic wear, 32 fields
each (title, brand, mrp, price, discount, rating, rating_count, highlights,
stock per size, seller, delivery/return days, image_url). Schema follows the
field set real scraped e-commerce datasets use.

Brands are invented. Do not swap in real brand names — the data is fabricated.

Images are SVG placeholders generated by scripts/generate_placeholders.py into
backend/static/products/. Locally generated on purpose: an external image host
that is slow or blocked turns the storefront into broken icons at demo time.
Replace image_url with real photographs to upgrade.

After editing the catalog, re-run scripts/ingest_catalog.py.

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
