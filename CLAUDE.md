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
- [x] ChromaDB populated with catalog (84 products, 384 dims, local MiniLM)
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
- [x] Storefront redesign: heritage-luxe design system, real Pexels photos,
      /shop filters + sort, /search, "You may also like", checkout with address
- [x] Retrieval rebuilt (bot/retrieval.py): eval 46/46
- [x] Conversational assistant with tools, memory and fallbacks (bot/assistant.py)
- [x] Second collection: 52 more products by function, plus footwear and bags (84 total)
- [x] Owner training (/admin/training) and customers' personal "Not for me"

## Storefront (redesign, 21 Sep — the whole website UI is now Dev A's)
- Tailwind is COMPILED, not the CDN script, so the site styles with no internet.
  After any template change, from backend/:
    npx -y tailwindcss@3.4.17 -c tailwind/tailwind.config.js -i tailwind/input.css -o static/css/site.css --minify
  Component classes (.btn-primary, .field, .panel, .display ...) are in
  tailwind/input.css. Fonts are self-hosted woff2 in static/fonts/.
- Shared Jinja setup is templating.py (inr filter, whatsapp_url(), NAV).
- search.py serves site search, similar products and recommended-for-you from
  the SAME Chroma collection as the bot; keyword fallback when Chroma is down.
- catalog.py holds the listing filters, sort and facet counts (plain Python).
- Checkout: GET/POST /checkout collects contact + delivery address
  (shipping.py validates: 10-digit mobile, 6-digit PIN), snapshots it on the
  order, then goes to Razorpay. Chat orders reuse the last saved address, or
  the order page asks for one.
- A product code like EW006 in a WhatsApp message shows and selects that
  product; the site's "Ask about this on WhatsApp" button pre-fills it.

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
Chat orders return the customer's browser to https://wa.me/<number> (the chat),
not to our site: a phone browser that had never visited the ngrok domain hit
ngrok's free-plan "You are about to visit" warning at the moment of paying.
Website orders still return to /payments/callback, which then offers a Back to
WhatsApp button.
Because chat orders skip our callback, a background reconciler (checkout.
reconcile_forever, started in main.py's lifespan) asks Razorpay every
PAYMENT_RECONCILE_SECONDS (default 5) about unpaid orders from the last hour.
Webhook = fast path; reconciler = guarantee. Tests set it to 0.
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

Retrieval is bot/retrieval.py, one engine for the bot, the assistant's tools and
the website search. Chroma ranks the whole catalogue by meaning in one query;
gender, category, budget and stock are then applied in Python from the
catalogue. Garment words map onto categories that exist (checked at import:
"jacket" -> Nehru Jacket/Waistcoat, "kurta" for a woman -> Kurti/Salwar Suit).
A garment after "my" is context, not the request ("jacket over my kurta").
So is one after "match"/"with" ("bag to match a lehenga"); it still sets the
gender ("shoes to go with my sherwani" -> men's). Shoes, bags and safas appear
only when asked for, never in an open "haldi outfit" request. A function word
(haldi, garba, reception...) puts products tagged for it first.
"Me and my wife"/"couple" shows both genders. If nothing passes, filters relax
(budget, then category) and the reply says so. Open-ended gibberish returns
nothing (MAX_DISTANCE). Embeddings alone never respect hard constraints: "groom
outfit" once ranked a women's kurta set first.
Chroma's HNSW index is approximate: asked for all 84 items it skipped EW080, so
rank() scores any skipped item exactly from its stored vector.
Verify: scripts/eval_retrieval.py (46 phrasings, every result must fit, plus
every owner ✓/✕ rating; the first RAG version scored 8/10 on an easier 10-case set).

## Assistant (bot/assistant.py)
Free text goes to an LLM with tools (Groq, OpenAI-compatible): search_products,
get_product_details, get_my_orders, get_my_cart, add_to_cart, send_reply. It asks
one or two questions when a request is vague and searches when it is not.
Exact commands ("2", ADD 42, CART, CHECKOUT, BUY, product codes) never reach it.
Trust boundaries, all enforced in code:
- product lists, prices, orders and cart contents are rendered by code;
  send_reply only names product ids, which must exist
- tools are bound to the sender's user_id; the model cannot pass one
- add_to_cart needs the customer's current message to ask for it and a size
  they typed; payment is always CHECKOUT -> Pay Now
Memory: sessions.messages (last 12; 8 sent per call), added by an idempotent
ALTER in db.init_db. Models: LLM_MODEL then LLM_FALLBACK_MODEL (comma list,
default gpt-oss-120b, qwen3.8-27b). Groq free tier = 8,000 tokens/min PER
MODEL; a call is ~1,000-1,500 tokens, so rapid-fire messages hit it. On 429 the
next model is tried, then one short wait. Groq 400 "tool_use_failed" calls are
repaired from failed_generation. Anything else -> bot/chat.py plain search
(which also answers "my orders"/"my cart" without the LLM).
Real messages are answered in a background task (Meta gets 200 at once) and
duplicate Meta deliveries are ignored by message id.
Photos: every product shown on WhatsApp (lists, a pick, a detail answer) is sent
as its photo with a code-built caption (name, price, MRP, one line, link).
Photos are uploaded once to WhatsApp's media API and the id cached in
backend/.media_cache.json (gitignored, renewed after 25 days); a failed photo
falls back to its caption as text. Order: intro text, photos, footer.
Facts: "what's my total"/"my cart"/"my orders" (short, no add/remove) are
answered by code before the model. The model gets the real cart in its
context, and any Rs amount it writes must appear in the catalogue, the
customer's words or a tool result, else it is told to retry, then the sentence
is dropped. (Live, it once said "total Rs 17,498" for a Rs 10,798 cart.)
Products it names in its own words get the real list/photo attached.
Verify: scripts/test_assistant.py (55 checks, scripted fake model, no quota).
Other suites blank LLM_API_KEY so they stay deterministic.

## Training (backend/training.py, /admin/training)
Not fine-tuning: the model never changes; what it is shown does.
- Every free-text answer is logged (bot_replies: question, reply, product ids).
- The store owner (email in ADMIN_EMAILS in backend/.env; anyone else gets 403)
  rates on /admin/training: ✓/✕ per product re-ranks that product for questions
  whose embedding is similar (cosine >= 0.75; "saree for office" ~ "office
  saree for work", not "sherwani for my wedding"), in the bot AND site search.
  👍 on a reply makes it a few-shot example for similar questions; a note on
  👎 becomes a rule in the prompt. "What it has learned" lists all, with Undo.
- Customers only hide items for THEMSELVES: the "Not for me" button under a
  WhatsApp photo, or typed ("not the second one") via the hide_product tool.
  Never affects other customers. "Choose" = typing the number.
- The feedback table stores the question's embedding; owner rules cache 30 s.
Verify: scripts/test_training.py (28 checks; deletes its own ratings).

## Follow-up handling
backend/selection.py parses a reply against last_products_shown. It requires the
WHOLE message to match a selector pattern, never a substring: "2 piece kurta set"
and "under 3000" contain digits but are searches. Out-of-range numbers are
treated as searches too.

sessions.selected_product holds the chosen item so a later BUY knows what it
means; starting a new search clears it. Razorpay's Pay Now button will read the
same field.

## Catalog
data/products.json — 84 products, men's and women's Indian ethnic wear, 32 fields
each (title, brand, mrp, price, discount, rating, rating_count, highlights,
stock per size, seller, delivery/return days, image_url). Schema follows the
field set real scraped e-commerce datasets use.

Brands are invented. Do not swap in real brand names — the data is fabricated.

Images are Pexels photos, stored locally in backend/static/products/{id}.jpg
(900x1125, 4:5) so the shop renders offline at the venue. Each was picked by
hand from contact sheets (scripts/fetch_photos.py candidates → review →
data/photo_choices.json → fetch_photos.py apply, which also writes image_credit).
Where the best photo's colour differed, the product TEXT was changed to match
the photo (EW005, EW009, EW010, EW018, EW026, EW027, EW032), so the page, the
photo and the bot agree. The SVG placeholders remain as a fallback only.

Second collection (22 Sep): EW033-EW084, 52 pieces chosen by function (haldi,
mehendi, sangeet, wedding, reception, festive incl. garba/Onam, office) plus
footwear (UK sizes; "ADD 8" matches "UK 8"), bags and a safa. New categories:
Indo-Western, Dhoti Kurta Set, Sharara & Gharara, Co-ord Set, Footwear, Bags,
Headwear. Text was written after choosing each photo (e.g. EW039 is midnight
teal, not the navy searched for). catalog.OCCASION_GROUPS are these functions
and drive the shop's "Function" filter and the home page row.

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
