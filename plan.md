# Munim: product plan (from 27 Sep 2026)

Munim goes from a college minor project (one ethnic-wear shop) to a product that real
brands use. The first real client is a friend's Shopify store selling T-shirts and
workout clothes.

## How to use this file

- **One chat per phase.** Open a new chat and paste that phase's *starter prompt*
  (at the end of each phase). The chat then plans the phase in detail before writing
  code.
- **This file is the source of truth** for goals, decisions and the order of work. When
  a phase finishes, update the status board, add any new decisions to the decision log,
  and record what changed in that phase's section.
- The September sprint plan is kept at
  [docs/archive/plan-sprint-19-23-sep.md](docs/archive/plan-sprint-19-23-sep.md).
  [WORK_SPLIT.md](WORK_SPLIT.md) describes the old two-developer split. **The project is
  now solo**, so its file-ownership rules no longer apply.
- Research behind the decisions was done on 27 Sep 2026 and checked against official
  docs where marked. The detailed findings, with code references and sources, are in
  [docs/research/2026-09-27/](docs/research/2026-09-27/):
  - bot-quality root causes (P2, P7);
  - models, voice and image search (P2, P6);
  - the Shopify connector (P3, P5);
  - Meta onboarding (P3, P5).

  **A phase chat should read the matching file.** Prices and model line-ups change
  quickly, so re-check them at the start of the phase that uses them.

## Status board

| Phase | What | Focused days* | Depends on | Status |
|---|---|---|---|---|
| P0 | Cleanup and safety net | 0.5 | — | done 28 Sep |
| P1 | Multi-store foundation (tenancy, products in Postgres, pgvector) | 7 | P0 | not started |
| P2 | Bot quality, part 1 (eval harness, fixes, works for any catalogue, model choice) | 12 | P1 | not started |
| P3 | Shopify connector, deployment, pilot go-live | 8.5 | P1, P2 | not started |
| P4 | Brand dashboard and minimal human hand-off | 5 | P3 | not started |
| P5 | Order status, exchanges, abandoned-checkout recovery | 6 | P3 (P4 helps) | not started |
| P6 | Voice notes and image search | 5.5 | P2, P3 | not started |
| P7 | Bot quality, part 2 (with real pilot traffic) | 6.5 | pilot live | not started |

\*Focused working days, estimated. The September sprint moved faster than estimates like
these, so treat them as an upper bound. **About 51 focused days in all.** The friend's
customers can start using the bot after **P3** (about 28 days in). P4 and P5 complete
the launch scope agreed on 27 Sep, and P6–P7 follow.

**Honest note on the 2–4 week hope:** everything agreed does not fit in 4 weeks solo.
The fastest route to real customers is P0 → P1 → P2 → P3. If time is short, cut from P2
first the items marked *(can wait)*.

## Where we are (27 Sep 2026)

- Phase 1 (minor project) is complete and graded. All suites were green on 27 Sep:
  - cart 126, assistant 79, payments 61, store 36, training 28, live 20, linking 18, live.js 15;
  - retrieval eval passes, and owner ratings hold 10/10.
- The code assumes **one shop** throughout:
  - catalogue in `data/products.json`;
  - one Chroma collection;
  - one WhatsApp token from env;
  - search vocabulary and prompts written for Indian ethnic wear (see P1 and P2).
- Bot quality has real problems. 54 logged conversations were analysed on 27 Sep, and
  the findings are summarised in P2. The biggest causes:
  - intent is guessed by stacked English regexes;
  - actions are described in the model's free text and only checked afterwards;
  - there is no structured "what the customer is asking for right now";
  - colour, fit and occasion are not filters;
  - the model is small (`gpt-oss-20b`, low reasoning).
- Customer-discovery interview guide: [docs/interview.md](docs/interview.md).
- The research paper is parked for about 2 months.

## Goal and pilot

- **Product:** a WhatsApp shopping assistant that any brand connects to its store.
  - It answers with real prices and stock from the brand's catalogue.
  - Checkout happens in the brand's own checkout.
  - It handles after-sale questions, brings back abandoned checkouts, and hands hard
    cases to a person.
  - The brand gets a dashboard showing the revenue it drove.
- **Edge over Meta's free Business AI** (launched in India, May 2026, inside the WhatsApp
  Business *app*):
  - grounded prices, stock and actions;
  - a real cart that goes into the brand's own checkout;
  - order status and exchanges;
  - recovery of abandoned checkouts;
  - hand-off to a person;
  - revenue attribution.
- **Pilot client:** a friend's Shopify store (T-shirts, workout clothes). The business is
  in India and unregistered.
- **Later clients** may run Next.js or other custom sites, so the core must not assume
  Shopify.
- **Demo for prospects:** the current ethnic-wear shop stays as demo brand #0.
- **Budget:** hosting at most about ₹1,000/month; LLM costs kept in the low hundreds.

## Decision log

| # | Decision | Chosen | Options considered | Why |
|---|---|---|---|---|
| 1 | Product direction | Multi-brand WhatsApp assistant; pilot on the friend's Shopify store | Stay a college demo; wedding/family niche first; photos→catalogue first | A real client is available now. Wedding/family and photos→catalogue go to the backlog |
| 2 | Where vectors live | **pgvector** in the same Postgres; Chroma retired | Keep Chroma (one collection per store) | Products move into Postgres anyway. One SQL query filters by store, price and stock and ranks by meaning. No two databases to keep in sync, one service to host |
| 3 | Pilot WhatsApp number | **New prepaid SIM owned by the friend's brand**, registered in *his* Meta portfolio | His current number (needs coexistence → needs us to be a Tech Provider); the Meta test number (reaches only 5 allow-listed phones) | Fastest legal route; no Tech Provider needed |
| 4 | How the pilot's Meta setup is done | **By hand:** the Meta app is created *inside his* business portfolio, and he adds Saumya as admin | App in our portfolio (fails with error 200 without Advanced Access); Tech Provider + Embedded Signup | Works today with no app review. Tech Provider comes later (backlog) |
| 5 | Shopify integration | **A Shopify app** ("plugin"), one custom-distribution app per brand from the Dev Dashboard. Standalone (not embedded in Shopify admin). Built with Shopify's official `shopifyapp` Python package, plus one theme app extension for the storefront button | Public App Store app now (review, billing rules); legacy admin custom app (can't be created since 1 Jan 2026); only reading the public `/products.json` | No review needed for one store. Same codebase serves N brands with N small config files. Move to one public app at about 10+ brands |
| 6 | Checkout for Shopify brands | **Storefront API cart → `checkoutUrl`**, sent as the Pay button; cart permalink as fallback | Our Razorpay page | The brand's own gateway, COD and discounts take the money. Munim never touches it |
| 7 | Non-Shopify brands | A `CommerceConnector` interface: Shopify first; later WooCommerce and a feed/API connector for Next.js sites (Google Merchant feed or our REST ingest; our Razorpay checkout on the brand's own keys) | Shopify only | The friend is on Shopify, but future clients may not be |
| 8 | Launch scope | Search → cart → Shopify checkout, **plus** order status and exchanges, abandoned-checkout recovery, brand dashboard with metrics, minimal hand-off | Core only | Chosen by the owner on 27 Sep. Hand-off was added because an API-only number cannot be answered from the phone app |
| 9 | Main LLM | **Not fixed yet: chosen by the P2 eval harness.** Expected winner: OpenAI **GPT-6 Luna** (function calling needs `reasoning_effort="none"`). Fallback on another provider: **Groq gpt-oss-120b** (paid tier). **Kimi K2.6 is included in the A/B test** | Kimi K2.6 as primary (owner's suggestion); Kimi K3; Gemini Flash-Lite; DeepSeek flash; Claude Haiku 4.5 | See "Why not Kimi as primary" in P2. Short version: about 5–10× the cost (₹2,000–3,200/month alone vs ₹200–400), slower (about 52 tokens/s on Moonshot's own API), and it does **not** do voice. Every cheaper candidate reads images too. It still gets a fair test on our own conversations |
| 10 | Voice notes | **Sarvam** speech-to-text (Indian languages, Hinglish "codemix" mode) with **Groq Whisper** as fallback | Groq Whisper only; OpenAI; Gemini audio; Deepgram | Best independent Hindi word-error rates; about ₹75/month at pilot volume. No LLM (Kimi included) removes the need for a speech-to-text step |
| 11 | Image search | **Hybrid:** product photos embedded with **Marqo-FashionSigLIP** (768 dims, self-hosted, Apache-2.0) in pgvector, plus a vision LLM that turns the photo into filters (category, colour, gender, print); the two rankings are merged | Vision LLM only; plain CLIP (much worse on fashion); hosted multimodal embeddings (Voyage, Gemini, Cohere) | Embeddings capture look (colour, print, cut). The LLM captures hard constraints and text in screenshots |
| 12 | Text embeddings | Upgrade from English-only MiniLM (384 dims). **Chosen by eval in P1** between **EmbeddingGemma-300M** (768 dims, multilingual, CPU) and **OpenAI text-embedding-3-small** (1536 dims, can be shortened) | BGE-M3 (heavier); Voyage-4-lite | The owner asked for a better model with more dimensions; both candidates are. The LLM will also write search queries in English, so Hinglish works whatever the embedder |
| 13 | Hosting | **Hetzner CX23** (2 vCPU / 4 GB, about ₹560/month) running Docker Compose (app + Postgres/pgvector) behind Caddy for automatic HTTPS; **CX33** (8 GB, about ₹870) once image-search models run. Try Oracle Cloud Always Free first if its signup works | Free tiers that sleep (Render etc.): webhooks and the payment reconciler need an always-on process; Railway; Lightsail Mumbai | Always on, within budget, same Docker setup as local |
| 14 | Our own business registration / Tech Provider | **Later**, done together. The pilot doesn't need it, because the friend's business owns his WhatsApp account | Now | Not blocking |
| 15 | Friend's business verification | **Recommended but optional:** free Udyam registration → Meta business verification. It raises the limit from 250 to 2,000 users/day for messages *we* start (templates) | Stay unverified | Replies to customers who wrote first are not capped, so the pilot works either way |
| 16 | Plan format | This file, one chat per phase | — | Owner's request, 27 Sep |

## Architecture target

```
 WhatsApp (per-brand number) ─┐                ┌─ ShopifyConnector (Admin GraphQL + webhooks, Storefront cart)
 Brand dashboard ─────────────┼→ Munim core ←──┼─ FeedConnector    (products.json demo brand; later Google Merchant feeds)
 Web chat widget (later) ─────┘   (per store)  └─ WooConnector / ApiConnector (later)
```

- **CommerceConnector** (`backend/connectors/`):
  - `sync_catalog()`, `apply_event()`, `stock()`;
  - `build_checkout(cart, buyer) -> url`;
  - `find_orders(phone)`, `order_status()`, `request_exchange()`;
  - `parse_webhook() -> normalised events` (product.updated, inventory.updated,
    order.created, order.updated, fulfillment.updated, checkout.abandoned, return.updated).
- **Tenancy:**
  - customers are per store (`users` unique on `(store_id, whatsapp_number)`);
  - carts, sessions and link tokens hang off `user_id`, so they are scoped automatically;
  - `store_id` goes directly on `orders`, `bot_replies`, `feedback`, `products`,
    `product_variants`, and the trace table;
  - the webhook finds the store from `value.metadata.phone_number_id`, and a
    `ContextVar current_store` carries it through the background task;
  - `whatsapp.py` sends with that store's credentials.
- **Products at variant level** (Shopify's model): `products` + `product_variants`.
  - Each variant is one colour × size pair, with its own stock, price and image.
  - Carts hold `variant_id`.
- **One LLM module** with a role per job (chat with tools, vision, speech-to-text, eval
  judge). Each role has its own provider, base URL, key and model, so trying Kimi or a
  vision model is a config change.
- **What the code must keep doing:**
  - code writes every fact (product lists, prices, cart, orders, totals, Pay button);
  - the model chooses what to do and writes the sentences around the facts;
  - tools are bound to the sender.

---

## P0: Cleanup and safety net (½ day)

**Goal:** a clean, tagged starting point, and nothing to lose when the schema changes.

**Scope**
- Push the 2 local commits (`6c7386c`, `2e14e39`). Commit `docs/interview.md` and this plan.
- Tag the college version: `v1-minor-project`. Do all new work on `product/*` branches.
- **Export the 54 logged conversations and 20 owner ratings.** Tables `bot_replies` and
  `feedback` go to `evals/raw/` as JSON; P2's eval harness is built from them. Do this
  before P1, because schema changes currently drop tables.
- Fix the ₹ encoding crash: `sys.stdout.reconfigure(encoding="utf-8")` in the test
  scripts. Add `scripts/run_all_tests.py`, one command for every suite.
- A README with what Munim is, the architecture figure, and a quick start.
- Delete the merged `a/*` branches and `feature/rag` (local and remote).

**Done when:** one command runs every suite green; the tag and the export exist.

**What was done (28 Sep)**
- `scripts/run_all_tests.py` runs the 8 suites and the retrieval eval in sequence (about
  80 s), one line per suite, with full output only for a failure. It checks first that
  Postgres and ChromaDB are reachable. `-v` streams everything; names pick a subset
  (`run_all_tests.py cart live`). All 9 were green on 28 Sep.
- The ₹ crash was not just a test problem. `print()` of a tool result containing ₹
  raised on a cp1252 pipe, so the assistant "failed" and the customer would have got the
  plain-search fallback. The fix is in every test script **and** in `backend/main.py`.
- `scripts/export_training_data.py` wrote `evals/raw/bot_replies.json` (54),
  `feedback.json` (20 owner ratings) and `manifest.json`. It uses plain SQL, so it
  still runs after P1 changes the models. Question embeddings are left out: they are
  recomputable, and P1 changes the embedder. **`evals/raw/` is gitignored and stays
  local**, like `backups/`. It holds customers' own messages and the repo is public
  (decided 28 Sep). The pilot's real conversations must never be pushed either. Back
  it up by hand; P2's harness reads it from disk.
- A full `pg_dump` is in `backups/` (gitignored, since it holds real phone numbers and
  addresses). Restore with `docker exec -i whatsappchatbot-postgres-1 pg_restore -U
  whatsapp -d whatsapp --clean --if-exists < backups/<file>.dump` (tested
  into a scratch database: 54 replies, 20 ratings, 7 orders came back).
- README with what Munim is, both architecture figures and a quick start;
  `backend/.env.example` gained `PEXELS_API_KEY`.
- Tag `v1-minor-project` is on `2e14e39`, the last graded commit.

**Starter prompt:** *"Read plan.md and do P0 (Cleanup and safety net). Show me the
commands before pushing or deleting branches."*

---

## P1: Multi-store foundation (≈7 days)

**Goal:** the core serves several stores safely. Products and their vectors live in
Postgres, and the demo brand still works exactly as before.

**Scope**
1. **Alembic.** Make a baseline of the current schema and replace the `ALTER` list in
   `db.init_db`.
2. **`stores` table.**
   - Platform and display name.
   - WhatsApp settings: `phone_number_id`, WABA id, app id, app secret, token. Secrets
     are encrypted with a key from env.
   - Connector config, currency, and the brand texts P2 needs (greeting, what we sell,
     policies).
   - Hand-off settings and business hours.
3. **Tenancy**, as in the architecture above.
   - Existing data becomes store #1 (demo), so all old suites keep passing.
   - New `scripts/test_tenancy.py` checks that two stores never see each other's users,
     carts, orders, products, vectors or training.
4. **Products in Postgres** (`products`, `product_variants`) at variant level.
   - `catalog.py` keeps its public functions but reads from the DB, scoped by store.
   - `FeedConnector` loads `data/products.json` as the demo store.
   - A small importer reads a Shopify store's public `/products.json` into a second
     store, **the friend's real catalogue**. This needs nothing from him and gives P2
     real T-shirt data.
   - Carts move to `variant_id`.
5. **pgvector.**
   - Postgres image → `pgvector/pgvector:pg15`.
   - An `embedding` column; retrieval becomes one SQL query per store: filters plus
     ordering by cosine distance, with an exact scan (no approximate index needed at
     these sizes).
   - Rewrite the four Chroma call sites: `bot/retrieval.py`,
     `catalog.get_similar_products`, `search.py`, and ingestion.
   - Drop the Chroma service.
6. **Embedding upgrade.**
   - Evaluate EmbeddingGemma-300M against text-embedding-3-small on the ethnic eval (46
     cases) plus a first T-shirt set, and keep the better one.
   - **Re-derive the two thresholds tuned for MiniLM**: `MAX_DISTANCE` 1.55
     (`bot/retrieval.py`) and `SIMILAR` 0.75 (`training.py`). They break silently
     otherwise.
7. **LLM module** with roles (see Architecture). `bot/chat.py` and `bot/assistant.py`
   both use it.
8. **WhatsApp per store.**
   - `whatsapp.py` takes credentials from the current store, with env as the demo
     fallback.
   - The media cache is keyed by store.
   - Message-id de-duplication moves from memory to a unique constraint in the DB.

**Out of scope:** Shopify API calls (P3); changes to what the bot says (P2).

**Files to start from:** `backend/models.py`, `db.py`, `repository.py`, `catalog.py`,
`bot/retrieval.py`, `search.py`, `common/embeddings.py`, `scripts/ingest_catalog.py`,
`whatsapp.py`, `routers/webhook.py` (`receive_message`), `docker-compose.yml`.

**Done when:**
- The demo store and the friend's imported catalogue coexist;
- `test_tenancy.py` passes;
- every old suite passes against the demo store;
- the retrieval eval is 46/46 with the new embedder, or any change is explained;
- no Chroma container remains.

**Open questions for the phase chat:**
- Keep a `stores` row for every future brand even before they connect?
- How is the encryption key rotated?
- Does the `/products.json` importer respect the store's rate limits?

**Starter prompt:** *"Read plan.md. Plan P1 (Multi-store foundation) in detail: the
schema, the migration order, and how every existing suite keeps passing. Then build it."*

---

## P2: Bot quality, part 1 (≈12 days)

**Goal:** the bot is measurably good, for any catalogue, and we choose the model with data.

### What the evidence showed (54 logged turns, 27 Sep)
All the logs come from the developer's own account, so treat them as a starting set.

| Failure | Root cause |
|---|---|
| Says it did something it didn't (added, removed, "here's your Pay Now button") | Actions are described in free text and checked afterwards by an English regex. There is no checkout tool. `remove_from_cart` has no quantity |
| Invents size options; gives fit advice from nothing; asks for a size again | The model sees real sizes only through a tool; nothing checks the sizes it names; there are no size charts |
| Budget or person carried over from an earlier request; category lost | No structured "current request". The last 8 raw messages are cut to 350 characters each, and filters come only from the model's own query text |
| Products that don't fit (colour, night vs day, groom vs guest) | Colour is not a filter at all; the occasion is dropped when the model's query leaves it out |
| "Reply CHECKOUT" instead of checking out | The prompt tells it to deflect; the CHECKOUT matcher misses "i wanna checkout" and typos |
| Sentences cut into fragments | Guards cut text after the fact instead of asking for a rewrite |
| Cart shown twice or counted confusingly | Model text and the cart block drawn by code are both sent |
| Typos change the route or lose a filter ("shervani", "chedckout", "sizr") | Every code-side parser matches exact words |
| Owner training can teach bad replies | 👍 copies the full rendered reply, fragments included; lookups use different keys from the ratings |
| Photos and voice notes get no answer at all | The webhook drops every message that isn't text |

What works and must be kept:
- lists, prices, cart and orders written by code (never wrong in the logs);
- tools bound to the sender;
- the rupee-amount check;
- `add_to_cart` requiring the customer's own words;
- relaxing filters with an honest note;
- the command routing ("2", ADD, CART, CHECKOUT).

### Scope, in order
1. **Conversation eval harness first** (`scripts/eval_conversations.py`, cases in
   `evals/<brand>/*.yaml`).
   - About 50 cases from the exported logs, plus about 40 T-shirt cases built from the
     friend's catalogue and his real customer questions.
   - It drives the real routing (`_handle_text`/`_handle_incoming`) against a throwaway
     user.
   - It records per turn: the path, tool calls, state changes, latency, tokens and cost,
     into a new **trace table** (the dashboard reuses it in P4).
   - Most checks are done by code: state diffs, claims backed by tool effects, sizes
     valid, filters met, no fragments. An LLM judge from a different model family covers
     only nuance, calibrated against the owner's own labels.
   - Needs Groq's paid tier; one full run costs about $1–5.
2. **Quick fixes.**
   - Answer photos and voice notes with one line until P6.
   - Checkout as a **tool** whose code reuses `_handle_cart_command('checkout')`; remove
     the deflection from the prompt; a wider, typo-tolerant CHECKOUT matcher.
   - **Action ledger:** state-changing tools record `{op, product, size, qty_before,
     qty_after}`, and code writes the confirmation line. Remove/set-quantity tools take
     quantity and size.
   - **Sizes as checked facts:** in-stock sizes shown in the context block; a size check
     like the rupee check; a `get_size_guide` tool backed by the brand's size chart.
   - **Typo tolerance:** correct words against a vocabulary built from the catalogue and
     the command words; never correct numbers or sizes.
3. **Work for any catalogue.**
   - A **brand-configurable prompt**: name, "what we sell" generated from the catalogue,
     policies, examples for the vertical.
   - Move the greeting, HELP, off-topic line, checkout description and `chat.py` intro
     into brand config.
   - Replace `_GARMENTS`, `_MENS/_WOMENS_GARMENTS`, `_OCCASIONS`, `_FUNCTIONS` with
     vocabularies built from each store's product types, tags and options. The ethnic
     words become an optional "vertical pack".
   - Replace the `EW\d{3}` product-code matchers with a per-store SKU/handle lookup.
4. **Enforced attribute filters.**
   - Colour family (from Shopify's colour option, else a mapping), plus fit, activity,
     fabric, sleeve and neck, extracted once per product.
   - They become `search_products` parameters, checked against the catalogue's real
     values.
   - The customer's own words are merged with the model's arguments, and every shown
     product must meet the filters (or the reply says the filters were relaxed).
   - **Hybrid search:** Postgres full-text + trigram + vector, merged by reciprocal rank
     fusion.
5. **Model A/B through the harness.**
   - Configs: gpt-oss-20b (today) · gpt-oss-120b (reasoning medium) · GPT-6 Luna
     (`reasoning_effort="none"`) · Kimi K2.6 (Moonshot or OpenRouter, thinking disabled,
     temperature fixed by Moonshot).
   - Pick the primary and fallback by code-check pass rate, then judge score, p95
     latency, and ₹ per 100 conversations.
6. **Fewer WhatsApp messages per answer.** From 1 Oct 2026 every delivered message costs
   money after 1,000 free per number per month. Today's intro + 3–4 photos + footer is
   5–6 messages.
7. *(can wait → P7)* current-request state, training fixes, removing the intent regexes,
   reply-structure rules.

**Why not Kimi as the primary (the owner asked, 27 Sep; checked against Moonshot's docs):**
- Moonshot now offers `kimi-k3`, `kimi-k2.6` and two coding models. The K2 series was
  retired on 25 May 2026 and K2.5 on 31 Aug. Groq stopped serving Kimi in March.
- **K2.6:** $0.95 in / $4.00 out per million tokens. It is thinking-on by default, with
  a fixed temperature and extra tool-loop rules, and runs at about 52 tokens/s on
  Moonshot's own API.
- **K3:** $3 / $15, with reasoning that cannot be switched off.
- **No Kimi model accepts audio**, so voice needs a speech-to-text service either way.
- Every cheaper candidate also reads images.
- It is China-hosted, which matters for customer names and addresses under India's
  DPDP Act.
- The fair way to settle it is the A/B test above, on our own conversations.

**Needs from the friend:** a product CSV export (Shopify → Products → Export; 5 minutes)
and 20–40 real customer questions from his DMs, with names removed (30 minutes).

**Done when:**
- The harness produces a report;
- the chosen model beats today's baseline on the code checks, with no regression;
- the T-shirt set and the ethnic set both meet their targets.

**Starter prompt:** *"Read plan.md. Plan P2 (Bot quality part 1) in detail, starting with
the eval harness design and the case format. Build the harness first and record a
baseline before changing the bot."*

---

## P3: Shopify connector, deployment, pilot go-live (≈8.5 days)

**Goal:** the friend's customers can message his WhatsApp number, see his real products,
and pay in his Shopify checkout.

**Scope**
1. **No-install demo (day 1).**
   - Read his live catalogue through Shopify's tokenless Storefront API, or the public
     `/products.json`.
   - Hand checkout to a **cart permalink**:
     `https://{shop}/cart/{variant}:{qty}?attributes[munim_conv]=…&ref=munim`.
   - This shows the full chat → checkout loop before he installs anything.
2. **Dev store.**
   - Create a free development store (Basic plan) in the Dev Dashboard and import his
     product CSV.
   - For the fast development loop, use a client-credentials token. It only works for
     stores in our own organisation and lasts 24 h.
3. **The app.**
   - Created in the Dev Dashboard with custom distribution for his store (the install
     link is his `myshopify.com` domain), and not embedded in Shopify admin.
   - Authorisation-code OAuth with **expiring tokens** (`expiring=1`, refresh token) from
     day one, using Shopify's official **`shopifyapp`** Python package (released 26 Aug
     2026; supports FastAPI).
   - Tokens are stored encrypted per store.
   - Tick the protected customer data fields used (phone, name, address, email). Custom
     apps get them without review.
   - **Scopes:** `read_products`, `read_inventory`, `read_orders`, `read_fulfillments`,
     `read_customers`, `read_returns`, `write_returns`,
     `unauthenticated_read_product_listings`, `unauthenticated_write_checkouts`,
     `unauthenticated_read_checkouts`.
   - One repo serves many brands through one `shopify.app.<brand>.toml` per brand.
4. **Webhooks** (declared in the toml; one FastAPI route).
   - **Topics:** `app/uninstalled`; `products/create|update|delete`;
     `inventory_levels/update`; `orders/create`; `orders/updated`;
     `fulfillments/create|update`; `checkouts/create|update`;
     `returns/request|approve|decline`; `bulk_operations/finish`.
   - **Compliance topics:** `customers/data_request`, `customers/redact`, `shop/redact`.
   - **Handling:** verify HMAC over the raw body with that brand's secret; return 200
     within 5 s; de-duplicate on `X-Shopify-Webhook-Id`; process in the background.
5. **Catalogue sync.**
   - First sync with `bulkOperationRunQuery` (products, variants, stock, all image URLs;
     image search in P6 needs the URLs) → Postgres → embeddings.
   - Kept current by webhooks, plus a nightly reconcile.
6. **Checkout.**
   - Mint the brand's Storefront token with `storefrontAccessTokenCreate`.
   - At CHECKOUT, call `cartCreate` with the lines, `buyerIdentity {phone, countryCode: IN}`
     and attributes `{munim_conv, munim_store}`.
   - Send `checkoutUrl` as the Pay button, and record `cart_token`.
   - **Attribution:** match `orders/create` by cart/checkout token first, then note
     attributes, then phone.
   - Fall back to the permalink if the Storefront API throttles. We can't send the
     buyer's IP; test this on the dev store.
7. **Theme app extension (app embed).**
   - A floating "Chat on WhatsApp" button, pre-filled with the product.
   - A WhatsApp opt-in checkbox near the cart, written to cart attributes via
     `/cart/update.js`.
   - The owner switches it on through a deep link.
8. **The friend's WhatsApp**, set up by hand (Meta checklist below).
   - Bump the Graph API from **v21.0** (retired 21 Jan 2027) to v25/v26.
   - **Verify Meta's `X-Hub-Signature-256`** with that store's app secret. The webhook
     doesn't check it today.
   - Submit message templates now (up to 24 h review): `order_confirmed`, `shipped`,
     `exchange_update` as utility; `abandoned_checkout` as marketing.
9. **Deploy.**
   - Dockerfile, and Compose (app + Postgres/pgvector) behind Caddy for HTTPS on a
     domain, on Hetzner CX23.
   - Nightly `pg_dump` backup; GitHub Actions running every suite; secrets in env.
   - Remove `/dev/send-test`. Publish a Munim privacy-policy page (Meta's Live mode needs
     one).
   - The payment reconciler and Razorpay stay for the demo brand.

**Meta checklist for the friend's number** (about 1.5 h of his time over 1–2 days, plus
about half a day for Saumya):

| Who | Step |
|---|---|
| Friend | 1. Facebook account with two-factor authentication on |
| Friend | 2. Create a business portfolio at business.facebook.com. Use the name that matches his store and website |
| Friend | 3. Add Saumya as admin |
| Friend | 4. New prepaid SIM, not on WhatsApp (KYC activation takes about a day) |
| Friend | 5. Be reachable for the OTP |
| Friend | 6. Add an INR card in Billing Hub. Without one, replies stop after 1,000/month from 1 Oct 2026 |
| Saumya | 7. Create a Business-type Meta app *inside his portfolio* (an app in our own portfolio gets error 200 on his account) |
| Saumya | 8. Add the number with display name, category and timezone; verify it by SMS or voice |
| Saumya | 9. Call `POST /{phone_number_id}/register` with a 6-digit PIN. Keep the PIN safe: 10 tries per 72 h |
| Saumya | 10. System user (Admin) with the app and the WhatsApp account assigned; generate a token that never expires with `business_management`, `whatsapp_business_management` and `whatsapp_business_messaging` |
| Saumya | 11. Webhook callback URL and verify token; subscribe to `messages`; call `POST /{WABA_ID}/subscribed_apps` and confirm with GET |
| Saumya | 12. Privacy policy URL, then switch the app to Live |

**Optional (recommended):**
- Free Udyam registration (Aadhaar OTP, about 20 minutes), then Meta business
  verification with the Udyam certificate. The legal name must match exactly.
- It raises his limits:
  - business-started messages: 250 → 2,000 unique users/day;
  - phone numbers: 2 → 20;
  - templates: 250 → 6,000.

**Done when:**
- A real phone messages his number, gets his products with photos, and reaches his
  Shopify checkout: on the dev store with Shopify's test gateway first, then live.
- The order confirmation arrives in chat, and the order is attributed.
- CI is green, and the backup has been restored once as a test.

**Open questions for the phase chat:**
- Which Shopify plan is he on? If it's Starter, test the Storefront cart and theme
  embeds there first.
- Do cart attributes reliably reach the order?
- Does throttling without the buyer's IP matter at his volume?

**Starter prompt:** *"Read plan.md. Plan P3 (Shopify connector and go-live) in detail.
Start with the no-install demo and the dev store, then the app. List what I need from my
friend and when."*

---

## P4: Brand dashboard and minimal hand-off (≈5 days)

**Goal:** the brand sees what Munim does for them and can take over a conversation.

**Scope**
- **Per-store login**, extending `auth.py`, with owner and agent roles.
  `/admin/training` moves in, scoped by store.
- **Inbox:** conversations with the transcript, the customer's cart and orders, and a
  reply box. Free-form replies only inside the 24 h window; outside it, a template.
- **Hand-off:**
  - Triggers: the customer asks for a person; the bot fails twice; refund, damaged or
    "payment deducted" words; an angry tone.
  - The bot tells the customer the usual reply time and pauses for that customer
    (`sessions.handoff_until`).
  - The owner is alerted by email, the dashboard and a utility template to their
    personal number (after they opt in).
  - "Return to bot" when solved. Meta's policy also requires automated chats to offer a
    clear way to reach a human.
- **Metrics from the trace table:**
  - chats, % answered by the bot, hand-offs;
  - carts, checkouts started, orders and revenue attributed to WhatsApp;
  - "asked for but not sold" (searches with no or relaxed results).
- **Settings:** brand texts, tone, policies (returns, exchanges, shipping), hours,
  hand-off rules, and a **size-chart CSV upload** (feeds `get_size_guide`).

**Done when:**
- Hand-off works end to end on a real phone;
- the dashboard numbers match counts taken straight from the database.

**Starter prompt:** *"Read plan.md. Plan P4 (Brand dashboard and hand-off) in detail,
including the screens and the hand-off states."*

---

## P5: Order status, exchanges, abandoned-checkout recovery (≈6 days)

**Goal:** after-sale questions and abandoned checkouts are handled on WhatsApp.

**Scope**
1. **"Where is my order?"**
   - Our own phone → orders index built from order webhooks (numbers normalised to
     E.164). Fall back to `customerByIdentifier(phoneNumber)` → `orders(customer_id)`.
   - Tracking comes from `Fulfillment.trackingInfo`, `estimatedDeliveryAt` and
     `deliveredAt`.
   - Proactive "shipped" and "out for delivery" messages use utility templates.
   - `read_orders` reaches only 60 days back, which is enough.
2. **Exchanges and returns.**
   - In chat: pick the order line, the reason and the wanted size (stock checked).
   - Code calls `returnRequest`, and the owner approves or declines in Shopify admin.
   - The bot tells the customer on `returns/approve|decline`.
   - Automatic exchange processing (`returnCreate` with exchange lines + `returnProcess`)
     can wait.
3. **Abandoned checkout.**
   - On `checkouts/update` with a phone and no `completed_at`, wait about 1 h and cancel
     if `orders/create` arrives with the same token.
   - If the checkout started from a WhatsApp chat within the last 24 h, send a normal
     message (cheap).
   - Otherwise send the **marketing** template, and only if we hold a recorded opt-in
     (the cart checkbox or in chat) naming the brand. Shopify's SMS consent isn't enough.
   - Handle error 131049: Meta's per-user marketing cap applies in India, so don't retry
     for 24 h.
   - Check that Shopify's own abandoned-checkout emails don't double up.

**Needs from the friend:** return and exchange rules set in Shopify (15 minutes); he
approves exchange requests in his Shopify admin.

**Done when:** each flow has been tested on the dev store and on a real phone.

**Starter prompt:** *"Read plan.md. Plan P5 (Order status, exchanges, abandoned-checkout
recovery) in detail, including the templates and the opt-in rules."*

---

## P6: Voice notes and image search (≈5.5 days)

**Goal:** customers can speak, or send a photo or screenshot, and still shop.

**Scope**
1. **Media plumbing.**
   - Handle webhook types `image` and `audio` (voice notes are `audio/ogg; codecs=opus`
     with `voice: true`).
   - `whatsapp.download_media`: GET the media id → a URL valid for 5 minutes → GET it
     with the store's bearer token (or use the webhook's `url` field when present).
   - Limits: images 5 MB, audio 16 MB.
   - Never keep photos or audio; keep only a text trace ("[voice] …", "[photo: black
     graphic tee]").
2. **Voice.**
   - **Sarvam** speech-to-text in codemix/translit mode (the REST call takes up to 30 s
     of audio per request; split longer notes). **Groq Whisper** (`language="hi"`) as
     fallback.
   - Cap at about 60 s. Echo "I heard: …" when the note is short or uncertain.
   - The transcript goes into `_handle_text`, so commands work by voice too.
3. **Image search.**
   - Product photos are embedded at sync with **Marqo-FashionSigLIP** (768 dims) into
     pgvector.
   - A customer photo gets a nearest-neighbour search *and* a vision-LLM call. The LLM
     extracts category, colour, gender, print, fit and any visible text as filters.
   - The two rankings are merged, stock and size filters are applied, and results show
     through the normal list (so "2", ADD and "Not for me" work).
   - Below a similarity threshold, say honestly that nothing is close.
4. **Hosting:** measure RAM and latency. Move to Hetzner CX33 (8 GB) or export the model
   to ONNX.

**Done when:**
- 10 real voice notes and 20 labelled customer photos or screenshots meet their
  hit-rate targets;
- median added latency is under 2 s.

**Starter prompt:** *"Read plan.md. Plan P6 (Voice notes and image search) in detail,
starting with the media download path and the labelled test sets."*

---

## P7: Bot quality, part 2 (≈6.5 days, after real pilot traffic)

**Scope**
- **Current-request state.** `sessions.brief`: `{for_whom, gender, category, colour,
  occasion, budget, size}`, reset on a new topic and checked against the customer's own
  words. It replaces raw history as the source of constraints.
- **Owner-training fixes.** Store the model's own wording, not the rendered reply. Let
  the owner rate "good wording" separately from "good products". Look up training with
  the same text it was rated with. Re-derive thresholds. Scope everything by store.
- **Retire the intent regexes** (`_wearer`, `_THEIR_FUNCTION`, `_who_hint`,
  `_ASKS_REORDER`, the `_is_lookup` phrase lists) once the harness shows no regression.
- **Reply-structure rules** instead of cutting sentences.
- **Grow the eval** from anonymised pilot conversations.

**Starter prompt:** *"Read plan.md. Plan P7 (Bot quality part 2) using the pilot's trace
data."*

---

## Backlog (after the pilot)

- **Meta Tech Provider:** register our business, verify it, pass App Review, build
  Embedded Signup **v4** (v2/v3 stop on 15 Oct 2026). This gives a "Connect WhatsApp"
  button and **coexistence** (brands keep their existing app number). Until verified,
  onboarding is capped at 10 new brands per 7 days.
- **A public Shopify app** (App Store review, Shopify billing, compliance) at about 10+
  brands. It has to be a new app, because distribution can't be changed.
- **WooCommerce connector:** one-click key approval, REST v3, signed webhooks, and
  `/checkout-link/`.
- **Next.js/custom connector:** a Google Merchant-format feed or our REST ingest, our
  signed order webhook, a JS snippet (button, opt-in, events), and Razorpay on the
  brand's own keys.
- A web chat widget; Instagram DMs; a browse-tracking pixel; back-in-stock and drop
  alerts; COD confirmation; a size advisor that learns from exchanges.
- A wedding/family board for ethnic-wear brands; photos → catalogue for offline shops.
- **Research paper** (about late November): grounding an LLM shopping agent with money.
  The P2 harness and pilot data are the evidence.

## Costs (pilot, per month, estimated 27 Sep)

| Item | Estimate |
|---|---|
| Hosting (Hetzner CX23 → CX33 from P6) | about ₹560 → ₹870 |
| Main LLM (GPT-6 Luna, or gpt-oss-120b) | about ₹200–500 (Kimi K2.6 would be about ₹2,000–3,200) |
| Speech-to-text (Sarvam) | about ₹75 (₹100 sign-up credit covers month one) |
| Domain | about ₹70 (about ₹800/year) |
| WhatsApp fees, **billed to the friend's card** | First 1,000 replies/number/month free, then ₹0.115 + 18% GST each; utility templates ₹0.115; marketing templates ₹0.8631 (+GST) |

## Main risks

- **Timeline:** about 51 focused days for everything; first customers after P3.
- **The GPT-6 Luna expectation** is untested on Hinglish and on our tools. The A/B test
  decides.
- **Shopify:** Storefront API throttling without the buyer's IP; cart attributes not
  always reaching the order (attribute by token instead); a Starter-plan store limits
  theme embeds.
- **Meta:** replies cost money from 1 Oct 2026; the per-user marketing cap means some
  recovery messages never arrive; an unverified business is capped at 250 users/day for
  messages we start.
- **Data protection (DPDP Act):** customer names, phones and addresses go to LLM
  providers. Prefer providers that don't train on inputs and aren't China-hosted.
  Meta's terms (23 Sep 2026) forbid using WhatsApp data to train AI models, except
  fine-tuning for the business's own use.
- **Solo bus factor:** CI, backups and this file are the safety net.

## Key sources (checked 27 Sep 2026)

- OpenAI GPT-6 Luna model page: https://developers.openai.com/api/docs/models/gpt-6-luna
- Kimi models and retirements: https://platform.kimi.ai/docs/models
- Meta charging service messages from 1 Oct 2026: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages
- 1,000 free replies and the payment-method rule (partner summaries): https://360dialog.com/blog/whatsapp-service-message-charging-october-2026/
- Meta messaging limits: https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits
- Meta opt-in rules: https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
- Tech Provider: https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers
- Coexistence: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
- Shopify official Python package: https://shopify.dev/changelog/build-shopify-apps-in-php-and-python-with-new-official-packages
- Shopify distribution: https://shopify.dev/docs/apps/launch/distribution/select-distribution-method
- Shopify protected customer data: https://shopify.dev/docs/apps/launch/protected-customer-data
- Storefront cart and checkout: https://shopify.dev/docs/storefronts/headless/building-with-the-storefront-api/cart/manage
- Cart permalinks: https://shopify.dev/docs/apps/build/checkout/create-cart-permalinks
- Theme app extensions: https://shopify.dev/docs/apps/build/online-store/theme-app-extensions/configuration
- Returns and exchanges: https://shopify.dev/docs/apps/build/orders-fulfillment/returns-apps/manage-exchanges
- Sarvam pricing: https://docs.sarvam.ai/api-reference-docs/pricing
- Fashion image retrieval benchmark (LookBench): https://arxiv.org/html/2601.14706v1
- pgvector limits: https://github.com/pgvector/pgvector
