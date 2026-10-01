# Munim

A WhatsApp shopping assistant for clothing brands. A customer messages the brand's
WhatsApp number in their own words ("something for a haldi, around 5k"), and Munim
answers with real products, photos, prices and stock from the brand's catalogue. It
keeps a cart and takes the customer to checkout, then tells them about their order.

Munim began as a college minor project (graded, September 2026) with one demo shop of
Indian ethnic wear. It is now becoming a multi-brand product. The first pilot is a
Shopify store selling T-shirts and workout clothes. [plan.md](plan.md) holds the roadmap
(phases P0–P7) and the decision log.

> The demo shop's 84 products and brands are invented. Product photos are from Pexels.

## What it does today

- **Chat that finds things.** An LLM with tools asks one or two questions when a request
  is vague, then searches. Retrieval ranks by meaning (vector search), then applies hard
  filters for gender, category, budget and stock in code.
- **Code writes every fact.** Product lists, prices, cart contents, totals and the Pay
  button are built by code, never written by the model. Tools are bound to the sender,
  so one customer cannot see another's orders.
- **Cart and checkout on both channels.** WhatsApp (`ADD`, `CART`, `CHECKOUT`, or plain
  words) and a web storefront share one cart. Payment runs through Razorpay (test mode),
  confirmed by a signed webhook and a background reconciler.
- **After the sale.** Order tracking, "buy it again", address capture in chat, and
  WhatsApp confirmation of website orders once the account is linked.
- **Owner training.** The shop owner rates answers on `/admin/training`. Ratings re-rank
  products for similar questions and become examples or rules in the prompt.

## Architecture

The system as graded in September (one shop):

![System architecture](docs/figures/fig_architecture.png)

Since then:
- **Payments** moved from Razorpay Payment Links to Standard Checkout (Razorpay orders
  plus our own `/pay/{order_id}` page).
- **Several stores (P1, Oct 2026).** Each store has its own WhatsApp number, customers,
  catalogue and training. A message is routed by the number it was sent to.
- **Products and vectors in Postgres.** They now live there with pgvector, read through
  a `CommerceConnector`: a JSON feed for the demo store, a Shopify shop's public
  catalogue for others. **ChromaDB is gone.**

Where it is going (see [plan.md](plan.md#architecture-target)):

```
 WhatsApp (per-brand number) ─┐                ┌─ ShopifyConnector (Admin GraphQL + webhooks, Storefront cart)
 Brand dashboard ─────────────┼→ Munim core ←──┼─ FeedConnector    (demo brand; later Google Merchant feeds)
 Web chat widget (later) ─────┘   (per store)  └─ WooConnector / ApiConnector (later)
```

**Stack:** FastAPI (async Python), PostgreSQL 15 with pgvector via SQLModel and
Alembic, EmbeddingGemma-300M embeddings (ONNX on CPU), an OpenAI-compatible LLM API
(Groq), WhatsApp Cloud API, Razorpay, Jinja templates with compiled Tailwind.

## Quick start

Needs Python 3.13, Docker, and Node (for one test suite and rebuilding CSS).
The commands are for Git Bash on Windows; on macOS/Linux use `.venv/bin/` instead of
`.venv/Scripts/`.

```bash
# 1. Database: Postgres with pgvector on 5432
docker compose up -d

# 2. Python environment
python -m venv backend/.venv
backend/.venv/Scripts/pip install -r backend/requirements.txt

# 3. Settings: copy the example and fill it in (each key is explained there)
cp backend/.env.example backend/.env

# 4. Run the app: storefront at http://localhost:8000
cd backend && .venv/Scripts/uvicorn main:app --reload --port 8000
```

The first start creates the tables (Alembic) and loads and embeds the demo catalogue.
It also downloads the embedding model once (about 310 MB). After editing
`data/products.json`, run `scripts/sync_catalog.py`.

The storefront, cart and checkout work with only steps 1–4. For the bot you also need:

- **An LLM key.** Set `LLM_API_KEY` (a Groq key works). Without it (or `OPENAI_API_KEY`), the bot answers with
  plain search instead of the conversational assistant.
- **WhatsApp.** A Meta app with the WhatsApp product, its token and phone number id in
  `.env`, and a public URL for the webhook (`ngrok http 8000`, then set
  `<url>/webhook` and your `WHATSAPP_VERIFY_TOKEN` in the Meta dashboard). The test
  number can only message the phones listed in the dashboard.
- **To try a conversation without a phone:** `scripts/simulate_webhook.py`.
- **Another brand:** `scripts/stores.py create` (with `--shop` for a Shopify store),
  then `scripts/sync_catalog.py <slug>` to import its catalogue. `scripts/stores.py
  set-whatsapp` stores its number and token, encrypted with `MUNIM_SECRET_KEYS`.

## Tests

One command runs every suite and the retrieval eval. The suites are cart, assistant,
payments, store, training, live sync, linking, tenancy (two stores kept apart) and the
browser-side live.js. Postgres must be running.

```bash
backend/.venv/Scripts/python scripts/run_all_tests.py             # everything, under 2 min
backend/.venv/Scripts/python scripts/run_all_tests.py cart live   # just these
backend/.venv/Scripts/python scripts/run_all_tests.py -v          # full output
```

The suites use fake WhatsApp, Razorpay and LLM clients, so they spend no quota and send
nothing. They share the development database and clean up only the rows they create.

`scripts/eval_embeddings.py` compares embedding models on our own data and re-derives
their thresholds.

## Repo layout

```
backend/           FastAPI app (run from here)
  main.py          app, lifespan (payment reconciler), static files
  migrations/      Alembic revisions (db.init_db runs them on every start)
  tenancy.py       which store the current work is for
  connectors/      CommerceConnector: feed (demo), Shopify public catalogue
  catalog.py, catalog_sync.py       reading and writing the per-store catalogue
  llm.py           the model and provider for each job (chat, vision, stt, judge)
  routers/         webhook (WhatsApp), store + browse + api (website), payments, admin
  bot/             assistant.py (LLM + tools), retrieval.py (search engine), chat.py (plain fallback)
  models.py, db.py, repository.py   Postgres schema and queries
  checkout.py, payments.py          orders and Razorpay
  templates/, static/, tailwind/    storefront
common/            shared schemas and the embeddings wrapper
data/products.json the demo catalogue (84 products)
scripts/           tests, evals, catalogue sync, store admin, data export
evals/raw/         logged conversations and owner ratings (P2's eval set; local only, gitignored)
docs/              research notes, report, figures; docs/archive/ holds old plans
```

`CLAUDE.md` has the detailed engineering notes: why each piece works the way it does,
and what went wrong live.
