# WhatsApp Shopping Assistant — Phase 1 (College Minor Project)

## Now (from 27 Sep 2026): read plan.md first
Phase 1 below is finished and graded. Munim is becoming a multi-brand product, with
the first pilot on a friend's Shopify store (T-shirts and workout clothes).
plan.md holds the phases (P0-P7), the decision log and a starter prompt for each
phase, and each phase is planned in its own chat. The project is now SOLO: the
Dev A / Dev B ownership rules below and in WORK_SPLIT.md no longer apply. The rest
of this file describes the code as it stands and stays accurate until a phase
changes it. P0 and P1 are done (see "Stores and tenancy (P1)" below). P2 has
started: every message is traced (see "Traces (P2)") and graded by the eval
harness (see "Conversation evals (P2)"); fixing what it finds is next.

## Context (September sprint)
2-person team, 7-day sprint. Deadline: 23 September 2026.
Dev A owns BUYING: FastAPI routers, WhatsApp, Razorpay, and the Postgres schema
(models.py, db.py, repository.py). Dev B owns FINDING: ChromaDB, embeddings, the
RAG/LLM pipeline (bot/chat.py), catalog.py, and the browse/search pages.

WORK_SPLIT.md holds the file-ownership table and the contracts (C1-C5) between
the two halves. Do not edit a file the other dev owns without asking them, and
never change a contract without syncing both devs. The sprint plan is archived at
docs/archive/plan-sprint-19-23-sep.md.

## Stack
- FastAPI (Python, async), SQLModel/SQLAlchemy + PostgreSQL 15 with pgvector,
  migrations by Alembic (backend/migrations)
- EmbeddingGemma-300M (ONNX, CPU) for product and question vectors
- An OpenAI-compatible LLM per role (backend/llm.py; Groq gpt-oss today)
- WhatsApp Cloud API v21.0, Razorpay (test mode)

## Repo layout
/backend  - FastAPI app, routers, models, migrations, connectors/ (shop platforms)
/backend/bot - assistant.py (LLM + tools), retrieval.py (search), chat.py (plain path)
/common   - shared Pydantic schemas (ProductMatch), embeddings.py
/scripts  - test suites, evals, sync_catalog.py, stores.py, export_training_data.py

Backend runs from inside /backend: `uvicorn main:app --reload --port 8000`.
Env is loaded from backend/.env by main.py (python-dotenv).

Every suite plus the retrieval eval, from the repo root (needs docker compose up):
    backend/.venv/Scripts/python scripts/run_all_tests.py   (names pick a subset, -v streams)
A new test script must call sys.stdout.reconfigure(encoding="utf-8") and exit
non-zero on failure; add it to SUITES in run_all_tests.py.

## Stores and tenancy (P1, 1-2 Oct)
Munim serves several stores. Store #1 is the demo (Kurta & Co.), which owns
everything made before stores existed.
- tenancy.py: the current store is a ContextVar; UNSET = the demo store, so the
  website, scripts and old tests need no changes. The webhook sets it from
  value.metadata.phone_number_id (no metadata = demo; a number no live store
  owns is ignored). Orders and logs take the store from the USER row.
- Per store: users (unique (store_id, whatsapp_number) and (store_id, email)),
  orders, bot_replies, feedback, products, product_variants. Carts, sessions and
  link tokens follow user_id. A link code only binds a number of its own store.
- Products: one row per style per colour (a Shopify product in 3 colours = 3
  products); variants are sizes, with stock, price and Shopify's variant id.
  `code` (EW006) is what customers type and what carts/orders call product_id.
  Cart lines gain variant_id once a size is known. Codes in chat match any
  store's prefix but only codes the current store sells.
- catalog_sync.py is the only writer of products; connectors/ fetch records
  (FeedConnector = data/products.json; ShopifyPublicConnector = a shop's public
  /products.json, 1 req/s, Retry-After honoured). catalog.py returns the same
  dicts as before, cached per store for 60 s and cleared by every sync.
- WhatsApp: whatsapp.py sends with the current store's credentials; only the
  demo store falls back to backend/.env. Secrets on stores are Fernet-encrypted
  (secrets_box.py; rotation in its docstring). Media ids are cached per store.
  Redeliveries are caught by processed_messages (Postgres), not memory.
- llm.py: a role per job (chat, vision, stt, judge), LLM_<ROLE>_* settings
  falling back to chat's. A key is only ever sent to its own endpoint.
- Webhook signatures (2 Oct): every change in a POST is checked against
  X-Hub-Signature-256 (HMAC-SHA256 of the RAW body) with the app secret of the
  store it is for: stores.wa_app_secret_enc, or WHATSAPP_APP_SECRET for the
  demo. A brand without a secret is never answered; the demo without one is,
  with a warning (local dev). A request where nothing verifies gets 403, so a
  wrong secret shows up as Meta retrying, not as silently lost messages.
  X-Local-Test changes how the reply is returned, never whether it is checked;
  simulate_webhook.py signs when WHATSAPP_APP_SECRET is set. Old suites set
  WHATSAPP_APP_SECRET="" (they post unsigned to the demo store).
- scripts/stores.py lists/creates/configures stores; sync_catalog.py <slug>
  imports one. A brand on Shopify: stores.py create <slug> "<Name>" --prefix XX
  --shop <domain>, then sync_catalog.py <slug>.
Verify: scripts/test_tenancy.py (62 checks: importer, and isolation of
customers, carts, orders, catalogue, vectors, search, codes, training, link
codes, credentials; webhook routing and signatures; secrets; redelivery).

## Traces (P2, 14 Oct)
backend/tracing.py records one turn_traces row per inbound message (migration
0005): the exit that answered it (path: command:add, selection, size, lookup,
assistant, search, button:pick, unsupported ...), every LLM call (model, ms,
tokens, cost from llm.PRICES, Groq's rate-limit headers), every assistant tool
call, each catalogue search (filters, results, what was relaxed), every check
that stepped in (guards: claim_retry/cut, amount_retry/cut, listing_cut,
off_topic, model_dropped, rate_wait, fallback, swallowed errors ...), the reply
as sent and how many WhatsApp messages it took. The P2 eval harness grades from
these; the P4 dashboard will count them. bot_replies stays the owner's
training log.
The trace is a ContextVar (like the store), begun and saved in the webhook
(_answer_and_send, _answer_locally, _trace_unanswered); code without one (the
website, scripts) records nothing. Recording must never change a reply: every
helper swallows its own errors, saving happens after the reply is built.
Channel: whatsapp, local (X-Local-Test), eval (set by the harness).
Verify: scripts/test_trace_neutrality.py plays 60 turns through every exit
with a fake model at llm.client: one saved trace per turn, and identical
replies, model requests and database state with every tracing internal
failing (tracing._faults). --record/--compare check against the code from
before tracing (both runs were identical on 14 Oct).

## Conversation evals (P2, 14 Oct)
scripts/eval_conversations.py plays evals/cases/<brand>/*.yaml through the real
routing (webhook._receive_change, answered inline, tracing.channel="eval") and
grades every turn by code from the trace and before/after state; evals/README.md
is the case format. Pieces in scripts/eval_harness/: cases.py (load, validate
against the catalogue, {order1}/{shown1} placeholders), state.py (a throwaway
customer per case, numbers +9900..., removed after), runner.py (scripted model
for preludes, the model under test for graded turns, payments and WhatsApp
faked), checks.py (expectations + 11 checks on every turn), pacing.py (free-tier
pacing; a per-day 429 stops the run, --resume continues), report.py, judge.py
(optional, --judge, a different model family). A config tests ONE model with no
fallbacks (evals/configs.yaml). Baselines in evals/baselines/; runs (model
outputs) in evals/runs/ are private. --dry plays everything with a canned
model, for free. Write expectations for the behaviour we want: a failing case
is a to-do, and the baseline records it.
Verify: scripts/test_eval_harness.py (44 checks, no quota). Baseline (after P2 step 2), gpt-oss-20b:
60/63 cases, 913/917 checks (was 47/63 before step 2; plan.md P2 has the history).

## Brands and vocabulary (P2 step 3, 14 Oct)
Nothing the bot says about the shop is written for one brand any more.
- brand.py: per-store texts from stores.brand (JSONB): name, role,
  description, what_we_sell, scope, example requests, one-size items,
  policies, off_topic, help_examples, vertical_pack. brand.system_prompt()
  renders the assistant's prompt; the greeting, HELP (webhook.help_text()),
  off-topic line, list intro (chat.py) and Razorpay description use it too.
  Unset keys are generated from the catalogue ("Men: t-shirts, joggers ...")
  or neutral. The demo's settings (migration 0006) are the old texts word for
  word: its prompt equals scripts/fixtures/demo_system_prompt.txt.
  Set them with: scripts/stores.py brand <slug> [--set key=value] [--file x.json]
- vocab.py: the words parse_query understands, per store, rebuilt when the
  catalogue reloads. From the catalogue: category names in their spellings
  (T-Shirts -> tee, tshirt, t shirt...), the noun a title ends with ("Gym
  Duffel Bag" -> bag), a gender when a category is all one gender, occasions
  from occasion and Shopify tags, add-on categories (accessories, bags, caps)
  shown only when asked for. A store whose brand names a vertical pack uses
  the pack instead (vocab.INDIAN_ETHNIC is the demo's, the word lists that
  used to be in retrieval.py), keeping only categories the store sells.
- Product codes are any store's PREFIX+digits, recognised only when the
  current store sells them (assistant._strip_codes, selection.find_product_code).
Verify: scripts/test_brands.py (40 checks: a second store built from
evals/catalogues/fitwear.json, an invented activewear brand, through the
Shopify importer; and the demo unchanged). evals/catalogues/fitwear.json is
made by scripts/fixtures/make_fitwear_catalogue.py.

## Colour, fabric and fit (P2 step 4, 15 Oct)
attributes.py reads them from a request and from each product, and
retrieval enforces them like gender and budget (Filters.colours / fabrics /
fits). Colours match by family (teal = blue + green, tangerine = orange,
"pila" = yellow); a product's from its colour field (Shopify's colour
option), else its name; fabric and fit from those fields, its tags and name.
A colour describing something else is ignored ("dupatta to go with my red
lehenga", "anything but black"). With nothing in that colour, search relaxes
it before dropping the garment, and the note says so ("We do not have
sherwanis in purple right now"). search_products takes colour / fabric /
fit; the customer's own words fill in what the model's query left out
(colour, fabric, fit, occasion, budget).
Verify: scripts/eval_retrieval.py (56 phrasings, 10 of them colour, fabric
and context cases), test_brands.py (black oversized tee, navy sports bra).

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
  The one exception: a request whose signature does not verify gets 403.
- Env vars: WHATSAPP_ACCESS_TOKEN, WHATSAPP_VERIFY_TOKEN, WHATSAPP_PHONE_NUMBER_ID
  and WHATSAPP_APP_SECRET (the demo store's WhatsApp), LLM_API_KEY / LLM_* (llm.py), EMBEDDING_PROVIDER,
  MUNIM_SECRET_KEYS (encrypts stores' secrets), RAZORPAY_KEY_ID,
  RAZORPAY_KEY_SECRET, DATABASE_URL. backend/.env.example explains each.
- Schema changes are Alembic revisions (autogenerate a draft from backend/:
  .venv/Scripts/alembic revision --autogenerate -m "..."; write backfills by
  hand); db.init_db() runs upgrade head on every start.

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
- [x] Docker Compose (Postgres with pgvector; Chroma removed in P1)
- [x] Catalogue in Postgres with vectors (84 products, EmbeddingGemma, 768 dims)
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
  the SAME vectors as the bot; keyword fallback when vector search fails.
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
checkout.py orchestrates; payments.py is a thin httpx wrapper over the Razorpay
REST API (no SDK: it is synchronous).
23 Sep: Payment Links were replaced by Standard Checkout (orders + our own
/pay/{order_id} page). Test mode allows only 30 payment links per account FOR
EVER, and this account spent them - live CHECKOUT answered "Razorpay 429: test
mode limit of 30 reached for payment_link" (24 of the 30 were Rs 1 links from
this repo's own test runs, which now create an order instead). Orders have no
such cap. start_payment creates a Razorpay order, stores its id in
payment_link_id and our page's URL in payment_link_url; templates/pay.html
loads checkout.js with the key id, that order and the amount from OUR database;
POST /payments/verify checks the signature Checkout returns (HMAC of
"order_id|payment_id" with the key secret) before recording anything. Orders
created before the switch still confirm through the old payment-link path.
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

## Delivery address in chat
A first chat order has nowhere to go, so CHECKOUT asks for the address instead
of creating an order (the website form is still there, and the order page still
asks if an order somehow lacks one). shipping.parse_chat_address reads a
courier-style message ("Aisha Khan, 9876543210 / 12 MG Road / Bengaluru 560038,
Karnataka"): phone, PIN and state are recognised for certain, the town is
whatever sits beside the PIN, the rest is the street. Anything still missing is
asked for one field at a time, the half-finished address living in
sessions.pending_address; CANCEL leaves the questions.
"My address" and "change my address" are answered in code too (the model
refused with "I can only assist with ethnic wear"); the confirmed address is
kept on users.shipping, which get_last_shipping prefers over the last order's,
so it can be changed before any order exists. Every order that carries an
address updates it.

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

## Schema layers
1. Postgres tables  - backend/models.py, real types and constraints
2. Pydantic contract - common/schemas.py, ProductMatch, validated in memory
3. Catalogue records - the dicts catalog.py returns (products.data JSONB, the
   shape of data/products.json); catalog_sync.normalise() guarantees every key

ProductMatch.from_raw() accepts both the catalogue key style (id/price/description)
and the contract style (product_id/price_inr/rich_description). The webhook
normalizes through it before storing, so neither side has to change first.

Products live in Postgres since P1, but orders still copy product_name and
price_inr at purchase time so an order records what was actually paid.

## Embeddings
EMBEDDING_PROVIDER in backend/.env selects the model, used by catalogue sync,
search and owner training through common/embeddings.py:
  gemma  (default) EmbeddingGemma-300M, 768 dims, multilingual, int8 ONNX on CPU
  minilm           all-MiniLM-L6-v2, 384 dims, English only (the Sept model)
  openai           text-embedding-3-small, 1536 dims, needs OPENAI_API_KEY
Both local models run on onnxruntime + tokenizers (no PyTorch, no Chroma).
Gemma takes a task prompt: embed_documents / embed_query / embed_similarity.
products.embedding_model records which model made each vector; on start,
db.init_db re-embeds rows from another model (~2 s for 84), and owner ratings
are re-embedded when read. MAX_DISTANCE and SIMILAR are per model in
common/embeddings.THRESHOLDS - a threshold from one model means nothing for
another. Chosen 2 Oct by scripts/eval_embeddings.py: same 46/46, Hinglish
requests 60% on target vs MiniLM's 22%, nonsense cleanly separated.

Retrieval quality improved markedly with the richer 32-product catalog, because
the embedded text now carries occasion, fabric, colour and fit vocabulary.
Price and stock are deliberately NOT embedded: they are filters, not meaning.

Retrieval is bot/retrieval.py, one engine for the bot, the assistant's tools and
the website search. One SQL query ranks the store's whole catalogue by cosine
distance (pgvector, exact scan, no index); gender, category, budget and stock
are then applied in Python from the catalogue. Garment words (vocab.py, per store) map onto categories that exist:
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
Chroma's approximate HNSW index used to skip items (EW080, and EW040 from
top-10s); the exact scan cannot. Distances match Chroma's: squared L2 = 2 x cosine.
Verify: scripts/eval_retrieval.py (56 phrasings, every result must fit, plus
every owner ✓/✕ rating; the first RAG version scored 8/10 on an easier 10-case set).

## Assistant (bot/assistant.py)
Free text goes to an LLM with tools (Groq, OpenAI-compatible): search_products,
get_product_details, get_my_orders, get_my_cart, add_to_cart, remove_from_cart,
hide_product, checkout, send_reply. It asks
one or two questions when a request is vague and searches when it is not.
Exact commands ("2", ADD 42, CART, CHECKOUT, BUY, product codes) never reach it.
Trust boundaries, all enforced in code:
- product lists, prices, orders and cart contents are rendered by code;
  send_reply only names product ids, which must exist
- tools are bound to the sender's user_id; the model cannot pass one
- add_to_cart needs the customer's current message to ask for it and a size
  they typed (a quantity above 1 too); remove_from_cart takes size/quantity
- paying: the `checkout` tool (only when their words ask to pay) ends the turn
  and the webhook runs the real CHECKOUT command (sizes, address, order, Pay
  Now). A reply that describes a payment button, or says "reply CHECKOUT" to
  someone asking to pay, is sent back once, then cut (P2 step 2, 14 Oct)
- cart changes are confirmed by code from a ledger the cart tools fill
  ({op, product, size, qty_before, qty_after}); the model's own "Added ..."
  sentences are dropped (webhook._ledger_lines)
- sizes are checked like rupee amounts: a size the product in focus does not
  come in is sent back, then cut; in-stock sizes are in the context block
- a send_reply Groq rejects because its arguments are bare words is repaired
  (the words become the message), not a fallback
Memory: sessions.messages (last 12; 8 sent per call), added by an idempotent
ALTER in db.init_db. Models: LLM_MODEL then LLM_FALLBACK_MODEL (comma list,
default gpt-oss-120b, qwen3.8-27b). Groq free tier = 8,000 tokens/min PER
MODEL; a call is ~1,000-1,500 tokens, so rapid-fire messages hit it. On 429 the
next model is tried, then one short wait. Groq 400 "tool_use_failed" calls are
repaired from failed_generation. Anything else -> bot/chat.py plain search
(which also answers "my orders"/"my cart" without the LLM).
Real messages are answered in a background task (Meta gets 200 at once) and
duplicate Meta deliveries are ignored by message id. 23 Sep: a "hi" was
answered in 0.09s and the answer never left the laptop — the POST to Meta timed
out on a sleeping Wi-Fi connection and _safe_send swallowed it, so the phone
showed nothing. whatsapp._send now tries three times (15s timeout, growing
pause) on a timeout, a dropped connection or a 5xx, and never retries something
Meta actually refused (190 expired token, 131030 recipient not allowed). The customer sees blue
ticks and "typing..." while that runs (whatsapp.mark_read_and_typing: one POST
with status=read + typing_indicator; WhatsApp clears it when the reply lands,
or after 25s), so a 3-5 second answer does not look like no answer.
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
It is a shop assistant, not a chatbot: asked "can u write a python code
for 2+2" it answered with a working snippet, so a reply that looks like
code (fences, print(, def, top-of-line import, SQL, HTML) is replaced with
the store's off-topic line (brand.Brand.off_topic). Ordinary off-topic
questions still get a kind line.
Profile (users.profile JSONB, idempotent ALTER): gender the customer stated
about THEMSELVES (rejected when another person is in the conversation: "only
male options" for a brother is the brother's) plus short notes, via
send_reply's customer_gender/remember_note; sizes chosen before are derived
from orders + cart, never stored. All three reach the prompt. /account shows
them with "Forget this". When neither the words nor the profile say who will
wear it, the prompt says so and the model must ask who + budget first.
Whose function it is is not who wears the outfit: "something to wear on a
wedding of my friend" was answered "is your friend a man or a woman?", so
_wearer() strips "<person>'s wedding / wedding of my <person>" before looking
for another person, and an explicit cue ("to wear", "for me", "what should I
wear") settles that the customer is the one dressing — then the model asks only
menswear or womenswear and the budget, and that answer is kept on the profile
(it used to be rejected, because a friend was mentioned). Without such a cue
("i wanna buy something for a night wedding") it still asks who it is for.
Seen live 22 Sep and now enforced in code: "yes"/a size answering our own
"shall I add it?"/"which size?" counts as asking (_consent); a reply claiming
"added"/"removed" without a successful cart tool is sent back once, then the
claim is cut; a product the model focuses on is selected, and "choose blush
pink one" selects by name (selection.parse_named_selection), so "46" next adds it.
Verify: scripts/test_assistant.py (96 checks, scripted fake model, no quota).
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

A message that is only a size ("xxl", "size 42", "8") or names one in a
sentence with a size word ("i think 11 size would be the best for me") sizes
the cart line waiting for one, else adds the picked item in that size: live,
the model promised "I'll add the mojari in UK 11" and CHECKOUT then found an
empty cart. "choose blush pink one" picks by name (parse_named_selection).
Sizes may be words (medium -> M), and a one-size product (bag, dupatta, stole)
takes any size word to mean "that one" — the model invented "(Sizes: S, M, L,
XL)" for a Free Size clutch, then refused to add it because "i think medium
would look good on her" never said "add". A size answering our own "which
size?" now counts as asking. A short question about a shown item ("would 1 be
a good gift for my wife?") selects it silently while the assistant answers, so
the size reply that follows has something to attach to. CHECKOUT matches how
people ask ("can i checkout pls", "i wanna pay", "ok lets proceed to payment",
and "chedckout": command words within two edits; selection.asks_to_checkout);
"pay for my sister" stays a search. A bare ADD takes the size from their last
message ("I like it in 42") or the one size our "shall I add it?" named.
Photos, videos, voice notes and files get one line saying we cannot read them
yet (until P6); reactions and stickers get nothing.

sessions.selected_product holds the chosen item so a later BUY knows what it
means; starting a new search clears it. Razorpay's Pay Now button will read the
same field.

## Buy it again
"can i buy one my old order agian" went to the model, which showed ONE product
and the two words "Added the." The customer's own history is now a shop of its
own, answered in code before the assistant sees it (_asks_to_reorder):
repository.past_purchases gives one entry per product, newest first, with the
size it was bought in; the bot shows four with photos, MORE pages back through
the rest, and a number puts that item straight into the cart in that same size
— nothing left to ask, because they already chose it once. "show me my previous
orders" stays the ORDERS lookup; only a buying verb ("buy"/"order"/"reorder")
reaches this path. The page on screen is sessions.reorder {offset, sizes},
cleared by the next list of any other kind, so a number only skips the size
question while a buy-it-again page is the list being looked at.
The stub came from the guard that stops the model writing out its own copy of
the list code is about to attach: one product name plus a rupee amount looked
like a listing, so "Added the Mint Embellished Box Clutch to your cart. Total
Rs 1,299." was cut at the name. It no longer cuts a confirmation when the cart
really did change this turn, and never leaves a fragment too short to be a
sentence.

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

After editing the catalog, run scripts/sync_catalog.py (the demo store; a slug for another).

## Known gaps
- RAG code lives in backend/bot/chat.py, not /rag as this doc states.
- /buy is a placeholder that writes an order row directly. Razorpay replaces it.
- bot/chat.py still uses the catalogue key style directly; adopting
  ProductMatch there would remove the last of the drift.
- Some conversation rules in bot/assistant.py are still worded for Indian
  weddings (_FUNCTIONS: "a wedding of my friend"); harmless elsewhere.
- Razorpay keys, PUBLIC_BASE_URL and the website are the demo store's only.
- The demo store accepts unsigned webhook POSTs until WHATSAPP_APP_SECRET is
  set in backend/.env; set it before the URL is public.

## Project history
- Meta WhatsApp Cloud API webhook handshake, subscription, inbound message handling,
    and outbound Graph API messaging completed.
- Added `scripts/simulate_webhook.py` for local conversational testing without ngrok
    or a physical phone.
- Added Docker Compose services for PostgreSQL and ChromaDB with persistent volumes.
- Added a 12-item ethnic wear catalog, OpenAI embedding ingestion, Chroma retrieval,
    and Phase 1 webhook-to-RAG response routing.
