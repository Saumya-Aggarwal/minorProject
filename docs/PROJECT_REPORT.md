# Context-Aware WhatsApp Shopping Assistant — Project Report

**Phase 1 · College Minor Project · Deadline 23 September 2026**
Last updated: 18 September 2026

---

## How to read this

This document serves three readers. Skip to yours.

| If you are… | Read |
|---|---|
| An evaluator seeing this for the first time | Parts 1–2, then Part 4 (what broke) |
| Dev B, returning to the project | Part 5 first, then Part 3 |
| Whoever opens this repo in three months | Part 3 and the appendices |

**Status in one line:** a customer messages a WhatsApp number, gets product
recommendations retrieved from a vector database, picks one by replying "2",
and places an order — and if they have linked their website account, the
assistant knows who they are and what they already own. Payments are the
remaining piece.

---

# Part 1 — What this is

## The problem

An online store's recommendations live on the website. The customer's attention
lives in WhatsApp. Bridging the two is not a UI problem — it is an identity and
retrieval problem:

1. **Retrieval.** "Something for my sister's mehendi, nothing too heavy" is not
   a database query. Keyword search fails on it. It needs semantic retrieval
   over product descriptions.
2. **Identity.** WhatsApp gives a business a phone number and a message body.
   Nothing else. No cookie, no session, no login. So a customer who is signed
   in on the website is a *complete stranger* to the bot by default.

Part 2 covers how each is solved. The second is the more interesting one, and
the part most likely to be asked about.

## What works today

```
Customer (WhatsApp)                          Storefront (browser)
        │                                             │
        │  "i need a sherwani for my wedding"         │
        ├────────────────────────────────────────►    │
        │  1. Royal Blue Wedding Sherwani ₹8,999      │
        │  2. Black Velvet Jodhpuri Suit  ₹7,499      │
        │  3. Wine Velvet Waistcoat       ₹2,399      │
        │                                             │
        │  "1"                                        │
        ├────────────────────────────────────────►    │
        │  Good choice — Royal Blue Wedding           │
        │  Sherwani, ₹8,999. Reply BUY to order.      │
        │                                             │
        │  "BUY"                                      │
        ├────────────────────────────────────────►    │
        │  Order #2 placed.  ───────────────────────► appears in /account
```

And in the other direction: a signed-in customer taps **Connect WhatsApp** on
the website, sends the pre-filled code, and from then on the assistant greets
them by name and can see their purchase history.

## Why these technology choices

| Choice | Reason |
|---|---|
| **FastAPI** | Async suits a webhook that fans out to Chroma, Postgres and the Graph API. Meta retries if you are slow to ack. |
| **PostgreSQL** | Orders and identity need constraints, transactions and money types. A partial unique index enforces "one active session per user" at the database level rather than in application code. |
| **ChromaDB** | Runs locally in Docker with no account and no cost. A managed vector DB would add an external dependency and a signup to a 7-day sprint. |
| **all-MiniLM-L6-v2, local** | Free, offline, and **cannot be rate-limited during a demo**. See Part 3.5 for why this mattered more than embedding quality. |
| **Jinja templates over a JSON API** | The storefront is a supporting piece, but every page has a `/api/*` equivalent. Replacing the templates with Next.js later means pointing it at the same API — no backend changes. |
| **Docker Compose** | Postgres and Chroma with persistent volumes, so the environment is identical on both developers' machines. |

---

# Part 2 — Architecture

```
                    ┌──────────────────────────────┐
                    │      Meta WhatsApp Cloud      │
                    │       (test number)           │
                    └──────────┬───────────────────┘
                               │ HTTPS webhook (via ngrok)
                               ▼
┌────────────────────────────────────────────────────────────────┐
│                    FastAPI  (backend/main.py)                   │
│                                                                 │
│  routers/webhook.py    routers/store.py    routers/api.py       │
│   GET  /webhook         /  /product/:id     /api/products       │
│   POST /webhook         /login /signup      /api/orders         │
│                         /account            /api/link/start     │
│         │                     │                    │            │
│         └─────────┬───────────┴────────────────────┘            │
│                   ▼                                             │
│    repository.py · auth.py · catalog.py · selection.py          │
│                   │                                             │
└───────────────────┼─────────────────────────────────────────────┘
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
┌──────────────────┐    ┌──────────────────────┐
│   PostgreSQL     │    │      ChromaDB        │
│  users           │    │  products (32 docs)  │
│  sessions        │    │  384-dim vectors     │
│  orders          │    │  + scalar metadata   │
│  link_tokens     │    └──────────────────────┘
└──────────────────┘              ▲
                                  │
                    common/embeddings.py (MiniLM, local)
```

## The identity bridge — the core idea

**A browser session cannot reach the bot.** This is not a limitation we chose to
work around; it is how the platform works. Meta's webhook payload contains:

```json
{"from": "919990327894", "type": "text", "text": {"body": "hi"}}
```

That is the entire identity signal. No cookie, no `Authorization` header, no way
to forward one.

The solution is to **bind identity once, then trust the phone number**:

```
1. Signed-in customer opens /account
2. Server mints a single-use token   →  link_tokens row, 10 min expiry
3. Page renders a deep link          →  https://wa.me/15551735724?text=LINK-a7f3c9
4. Customer taps, WhatsApp opens with the message pre-filled, they send it
5. Webhook sees a body starting "LINK-", validates the token
6. users.whatsapp_number is set on that account; the token is marked used
7. From now on, every message from that number is authenticated
```

Why this is defensible when the panel asks *"couldn't someone fake a phone
number?"*: the phone number alone is never trusted to establish identity. It is
trusted only **after** a one-time secret, generated for a logged-in session and
valid for ten minutes, arrives from that number. That is the same shape as an
email confirmation link.

### The merge problem

Step 6 hides the hardest part. By the time a customer links, they have usually
**already messaged the bot** — so a row already exists holding that phone number:

```
BEFORE LINKING                          AFTER LINKING
users                                   users
 ├─ #1 whatsapp: 919990327894  ← bot     └─ #2 whatsapp: 919990327894
 └─ #2 email: demo@example.com ← web           email:    demo@example.com
     └─ order: Brocade Silk Kurta              ├─ order: Brocade Silk Kurta
                                               └─ session history from #1
```

Row #1 must be folded into row #2: its orders and sessions re-pointed, then the
row deleted — all inside one transaction, because a half-applied merge would
leave orders referencing a user that no longer exists. `consume_link_token()` in
[backend/repository.py](../backend/repository.py) does this.

This is also where the first real bug appeared. See Part 4.2.

## The three "schemas"

A recurring source of confusion worth stating plainly — the word means three
different things here, and only one of them is enforced:

| Layer | Where | Validated? |
|---|---|---|
| Postgres tables | `backend/models.py` | **Yes** — types, FKs, CHECK constraints |
| Pydantic contract | `common/schemas.py` | **Yes** — in memory, at the boundary |
| Chroma metadata | dicts in `scripts/ingest_catalog.py` | **No. Not at all.** |

Chroma will happily store `{"naem": "..."}`. Nothing errors. The lookup in
`chat.py` then returns its default and the bot recommends *"Item at ₹0"*. This
is why `ProductMatch.from_raw()` normalises everything at the webhook boundary —
it is the only place a typo in the catalog can be caught.

---

# Part 3 — Subsystem detail

## 3.1 Webhook (`backend/routers/webhook.py`)

**GET /webhook** — Meta's verification handshake. Echoes `hub.challenge` as
plain text (not JSON) when `hub.verify_token` matches.

**POST /webhook** — the message pipeline:

```
payload → for each message
            ├─ starts with "LINK-"?  → _handle_link_code()   (bind account)
            ├─ matches a selector?   → _handle_follow_up()   ("2", "BUY")
            └─ otherwise             → RAG search
          → record the turn in sessions
          → _safe_send() the reply
          → ALWAYS return 200
```

**The invariant that matters: this endpoint must always ack 200.** A non-200
tells Meta delivery failed, and it redelivers the same message. If the failure
is deterministic — an expired token, say — every retry fails identically. We
observed exactly this; Part 4.1.

## 3.2 Persistence (`models.py`, `db.py`, `repository.py`)

Four tables. Design decisions worth knowing:

**`users` is one table for both channels.** Not `customers` + `bot_contacts`.
The entire point of linking is that the two resolve to one person; two tables
would recreate the duplicate-identity problem by design. A CHECK constraint
enforces that at least one of `email` / `whatsapp_number` is present.

**`price_inr` is `NUMERIC(10,2)`, never `float`.** Float money drifts —
`2499.0` can come back as `2498.9999…`, and a mismatch against Razorpay's paise
amount is an evening lost to a rounding bug.

**Order capture is idempotent.** Razorpay can deliver `payment.captured` more
than once. `mark_order_captured()` returns `True` only the first time, so one
payment sends one confirmation:

```python
repo.mark_order_captured("order_TESTRZP1", "pay_TEST1")   # True
repo.mark_order_captured("order_TESTRZP1", "pay_TEST1")   # False  ← replay
```

**A partial unique index enforces one active session per user**, so two messages
arriving together cannot create competing sessions:

```sql
CREATE UNIQUE INDEX idx_sessions_user_active
    ON sessions (user_id) WHERE status = 'active';
```

**Database failure must not silence the bot.** Every repository call from the
webhook is wrapped: a dead Postgres costs conversation *memory*, not the
conversation.

## 3.3 Retrieval (`backend/bot/chat.py`, `common/embeddings.py`)

Query → embed → Chroma similarity search → optional LLM phrasing → reply.

**What gets embedded decides what can be found.** The document per product is
built in `scripts/ingest_catalog.py`:

```
title . gender+category+subcategory . fabric/colour/pattern .
fit/sleeve/neckline . "Suitable for: Wedding, Festive, Diwali" .
description . highlights
```

**Price and stock are deliberately excluded.** They are filters, not meaning —
embedding the digits "2499" adds noise to the vector and helps no query.

**The provider is switchable** (`EMBEDDING_PROVIDER=local|openai`) through a
single module used by *both* ingestion and query. This matters: vectors written
by one model cannot be searched with another. Keeping the choice in one place is
what stops the two halves silently drifting apart. Switching requires re-running
ingestion, and `chat.py` detects the dimension mismatch and says so.

**Embeddings do not respect hard constraints.** The clearest example in this
project:

```
Query: "groom outfit budget 12000"

WITHOUT metadata filter          WITH gender filter
1. Teal Silk Kurta Set (Women) ✗  1. Royal Blue Wedding Sherwani  ✓
2. Royal Blue Sherwani            2. Black Velvet Jodhpuri Suit   ✓
3. Wine Velvet Gown (Women)   ✗   3. Wine Velvet Waistcoat        ✓
```

The wedding vocabulary overwhelmed the single word that mattered. No amount of
prompt engineering fixes this — the wrong products were already retrieved before
any LLM saw them. `_gender_filter()` narrows the search with Chroma metadata
when the query names hints from exactly one set.

## 3.4 Follow-ups (`backend/selection.py`)

The assistant ends every recommendation with *"Reply with an item number"*, so
"2" must mean the second item, not a search for the string "2".

**The parser requires the whole message to match a selector pattern.** Substring
matching would be a disaster:

| Message | Verdict | Why |
|---|---|---|
| `2` | selection | bare number in range |
| `the second one` | selection | ordinal phrase |
| `i will take the first one` | selection | lead-in + ordinal |
| `2 piece kurta set` | **search** | contains a digit but is not a selector |
| `under 3000` | **search** | out of range, and a budget |
| `5` (3 items shown) | **search** | out of range |

Tested at 17 selector phrasings recognised and 12 search phrases correctly
ignored.

`sessions.selected_product` holds the pick so a later `BUY` knows its referent;
starting a new search clears it, so a stray "BUY" cannot purchase something the
customer has moved on from.

## 3.5 Storefront (`routers/store.py`, `routers/api.py`, `templates/`)

Product grid, product detail, signup/login, account page with order history and
the Connect WhatsApp button. Passwords are bcrypt; sessions are signed cookies.

**Every page has a JSON twin under `/api`, and no logic lives in a handler.**
That is what makes "convert to a serious product later" real rather than
aspirational — the backend is the product, Jinja is its first client.

**Login failures return one message for both cases** ("Incorrect email or
password") so the form cannot be used to enumerate registered addresses.

## 3.6 Catalog (`data/products.json`)

32 products, men's and women's Indian ethnic wear, 32 fields each — modelled on
the field set real scraped e-commerce datasets use (title, brand, mrp, price,
discount, rating, rating_count, highlights, stock per size, seller, delivery and
return days, image_url).

**Brands are invented.** Real ones were rejected: fabricated catalog data under a
real company's name does not belong in a submitted project.

**Images are locally generated SVGs**, not stock photo URLs. An external image
host that is slow or blocked turns the storefront into a grid of broken icons,
and demo venues have unreliable wifi. Swapping in photographs later is editing
`image_url`.

### A dataset that was evaluated and rejected

`luminati-io/eCommerce-dataset-samples` (suggested mid-build) was checked:
Amazon, Walmart, Lazada, Shopee, Shein — general products, USD prices, real
brands, **no Indian ethnic wear**. Kaggle's *Fashion Product Images* set has the
right niche and real photographs, but only titles and attribute columns — no
descriptions. For semantic retrieval that is the wrong shape; there is almost
nothing to embed.

What was taken from the Bright Data samples was the **schema**, which this
catalog now mirrors. The principle: **for RAG, description richness matters more
than row count.**

---

# Part 4 — What broke, and what it taught

The most instructive part of the build. Each entry is a real failure with the
evidence that revealed it.

## 4.1 The webhook retry storm

**Symptom.** Messages arrived and were logged, but the bot never replied. The
same message appeared three times in the logs.

```
[whatsapp] send failed (401): {'error': {'code': 190, 'type': 'OAuthException'}}
INFO: "POST /webhook HTTP/1.1" 500 Internal Server Error
[webhook] message from 919990327894: hi          ← same message, again
INFO: "POST /webhook HTTP/1.1" 500 Internal Server Error
[webhook] message from 919990327894: hi          ← and again
```

**Cause.** Two faults compounding. The access token had expired (code 190), and
`send_whatsapp_message()` raised `HTTPStatusError`, which the handler's
`except (KeyError, TypeError, ValueError)` did not catch. FastAPI returned 500.
Meta read 500 as failed delivery and redelivered — into the same failure.

**Fix.** `_safe_send()` swallows send failures, and the outer handler catches
`Exception`. The endpoint now always acks 200. The error message also became
actionable:

```
[whatsapp] token expired — generate a new one in the App Dashboard
          (WhatsApp > API Setup) and update WHATSAPP_ACCESS_TOKEN in backend/.env
```

**Lesson.** A webhook must acknowledge receipt even when its own work failed.
Delivery and processing are separate concerns, and conflating them turns one
failure into an infinite loop. *Secondary lesson: an error message that names
the fix is worth more than one that names the error.*

**Follow-up.** The temporary dashboard token expires every 24 hours — meaning it
would have been expired on demo morning. Replaced with a System User token,
verified as permanent:

```
type: SYSTEM_USER   valid: True   expires: never
scopes: whatsapp_business_management, whatsapp_business_messaging
```

## 4.2 The merge that violated its own constraint

**Symptom.** Account linking failed at the exact moment it mattered — when the
customer had messaged the bot before linking, which is the natural order.

```
[repository] merging bot user 1 into web user 2
[webhook] link failed: IntegrityError('new row for relation "users" violates
          check constraint "ck_users_has_identity"')
```

**Cause.** The merge blanked the orphan row's phone number to free the unique
index *before* deleting the row. For that instant the row had neither email nor
phone — exactly what the CHECK constraint forbids.

**Fix.** Delete the row outright and flush, *then* assign the number:

```python
# Delete outright rather than blanking the number first: a row with neither
# email nor whatsapp_number violates ck_users_has_identity. The flush must
# land before the number is reassigned, or the unique index still sees the
# old row holding it.
db.delete(existing)
db.flush()
target.whatsapp_number = whatsapp_number
```

**Lesson.** Constraints fire on *intermediate* states, not just final ones. A
transaction that ends valid can still fail halfway. **And: this was caught by a
test that deliberately reproduced the realistic ordering.** A test that linked a
fresh account would have passed and the bug would have surfaced live.

## 4.3 numpy floats rejected by Chroma

**Symptom.** Ingestion crashed after the embedding step:

```
ValueError: Expected embeddings to be a list of floats or ints, a list of
lists, a numpy array... got [[np.float32(-0.0057018343), ...]]
```

**Cause.** The local model returns numpy `float32` arrays. `list(vector)` turns
the array into a list of `np.float32` *scalars* — which is neither a Python
float list nor a numpy array, so Chroma's validator rejected it.

**Fix.** `vector.tolist()`, which converts all the way down to Python floats.

**Lesson.** "Convert to list" and "convert to a list of native types" are
different operations, and library validators check the leaves.

## 4.4 Retrieval that ignored the important word

**Symptom.** `"groom outfit budget 12000"` returned a **women's** kurta set
first. `"jacket to wear over my kurta"` returned three kurtas and no jacket.

**Diagnosis, not guesswork.** The first hypothesis was that the embedded
document text was too noisy — it included `Price: Rs. 2499` and size lists.
That was tested directly by embedding both formats and comparing:

```
=== CURRENT (with price and sizes) ===   === CLEAN (no price/sizes) ===
jacket to wear over my kurta             jacket to wear over my kurta
  0.652  Brocade Silk Kurta      ✗         0.590  Brocade Silk Kurta   ✗
  0.590  Mustard Bandhani Set    ✗         0.579  Mustard Bandhani Set ✗
```

Cleaning the documents changed nothing. The hypothesis was wrong, and the real
cause was the model's handling of a compositional query — "kurta" appears in the
sentence and dominates "jacket".

**Fix (partial).** A richer catalog with occasion vocabulary improved most
queries substantially, and a **gender metadata filter** fixed the groom case
entirely. The jacket case remains imperfect.

**Lesson.** Test the hypothesis before acting on it — the obvious explanation
was wrong, and "fixing" it would have been wasted work. More broadly:
**embeddings rank, they do not constrain.** When a query contains a hard
requirement (men's, under ₹3000, in stock), that belongs in a metadata filter,
not in the hope that cosine similarity will respect it.

## 4.5 Two self-inflicted tooling errors

Included because they cost real time.

**A slice-based string replacement destroyed a file.** An attempt to patch
`ingest_catalog.py` by slicing between two markers produced an empty match, and
`str.replace("", new)` inserts between *every character*. The file was recovered
with `git checkout` and rewritten properly — a good argument for committing
before mechanical edits.

**A `\n` inside a shell heredoc became a literal newline**, producing an
unterminated f-string. Caught immediately by parsing the file rather than
assuming the edit worked:

```bash
python -c "import ast,pathlib; ast.parse(pathlib.Path('routers/webhook.py').read_text())"
```

**Lesson.** Verify after mechanical edits. A syntax check takes one second and
catches what the eye does not.

## 4.6 A stale document claiming work was done

`CLAUDE.md` listed "Webhook verification (GET) working" as complete before it
had been attempted, and later `.env.example` was missing five variables the code
already read. Both were corrected.

**Lesson.** A status file that lies is worse than no status file: it hides the
real blocker. `.env.example` drifting is the same failure — the file a teammate
copies from should be updated in the same commit as the code that reads the new
variable.

---

# Part 5 — For Dev B

## What changed in your territory

**`backend/bot/chat.py`** — two additive changes:
1. Embedding now goes through `common/embeddings.py` instead of calling OpenAI
   directly, so ingestion and query cannot drift apart. `EMBEDDING_PROVIDER`
   selects `local` (default) or `openai`.
2. `_gender_filter()` applies a Chroma metadata filter before the query. See
   Part 4.4 for the evidence.

`_polish_response()` is untouched and still uses GPT-4o-mini when
`OPENAI_API_KEY` is set. Without a key it falls back to the formatted product
list — retrieval still works, only the conversational phrasing is missing.

## The contract addition, agreed but not yet implemented

```python
get_product_recommendations(query: str, top_k: int = 3, user_context: str | None = None)
```

`user_context` carries the linked customer's name and purchase history for the
prompt. **Backward compatible** — until you add it, `_recommend()` in
`webhook.py` inspects the signature and omits the argument:

```python
if user_context and "user_context" in inspect.signature(
    get_product_recommendations
).parameters:
    return get_product_recommendations(query, user_context=user_context)
return get_product_recommendations(query)
```

So nothing breaks either way. Add the parameter when convenient.

## Open items in your area

- **Retrieval tuning.** "jacket to wear over my kurta" ranks a kurta above the
  Nehru jacket. A category filter (the metadata is already there) or a higher
  `top_k` would likely fix it.
- **"me and my wife"** filters to Women, because "wife" is a hint and "me" is
  not. Needs intent parsing, not keywords.
- **Embedding provider decision.** Currently local MiniLM. Switching to OpenAI
  is an env var plus a re-ingest. Local cannot be rate-limited during a demo,
  which is worth weighing against quality.
- **`ingest_catalog.py` and `chat.py` still use the raw Chroma key style**
  (`id`/`price`/`description`). `ProductMatch` in `common/schemas.py` accepts
  both styles, so this is not urgent, but adopting it would remove the last of
  the drift.

---

# Part 6 — Current state and what remains

## Done

- [x] Meta webhook verification and message handling
- [x] Outbound messaging, permanent System User token
- [x] Docker Compose: Postgres + ChromaDB with persistent volumes
- [x] Postgres schema: users, sessions, orders, link_tokens
- [x] 32-product catalog, ingested, 384-dim vectors
- [x] RAG retrieval with gender metadata filtering
- [x] Account linking with single-use tokens and orphan merge
- [x] Personalization for linked customers
- [x] Item selection ("2", "the second one") and BUY
- [x] Storefront: catalog, auth, account, order history
- [x] JSON API mirroring every page

## Remaining

- [ ] **Razorpay** — order creation, Pay Now interactive message, `payment.captured`
      webhook. Test account exists; keys not yet in `.env`. `create_order()` and
      `mark_order_captured()` are built and tested; `_place_order()` in
      `webhook.py` is the function to replace.
- [ ] Personalization on the *website* (recommendations for you) — the bot has
      it, the site does not
- [ ] Demo video, rehearsal

## Known limitations (state these before the panel finds them)

| Limitation | Note |
|---|---|
| No Alembic migrations | Schema changes drop and recreate tables. Fine while data is disposable. |
| `/buy` and `BUY` are placeholders | They write an order row directly. Razorpay replaces both. |
| Compositional queries | "jacket over my kurta" still imperfect. |
| Test number allow-list | Meta's test number only messages numbers registered in the dashboard (max 5). |
| 24-hour window | Free-form messages only deliver within 24h of the customer's last message. Outside it, a template is required. |

## Demo runbook

```powershell
# 1. Infrastructure
docker compose up -d
docker compose ps                    # both running

# 2. Backend
cd backend; .venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000

# 3. Public tunnel (separate terminal)
ngrok http 8000
# Update the Callback URL in the Meta dashboard to the new ngrok URL + /webhook
```

**Gotchas that have actually bitten:**
- ngrok's URL changes on every restart — the Meta webhook must be re-pointed.
- `.env` changes need a **uvicorn restart**; `--reload` only watches `.py` files.
- Every phone used in the demo must be on the dashboard's "To" list.
- Record a backup video. WhatsApp demos fail live more than any other integration.

---

# Appendix A — Environment variables

| Variable | Purpose |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Graph API auth. Use a System User token — it never expires. |
| `WHATSAPP_VERIFY_TOKEN` | Any string you invent; must match the Meta dashboard field. |
| `WHATSAPP_PHONE_NUMBER_ID` | `1370083336179112` — from API Setup, not the phone number. |
| `WHATSAPP_DISPLAY_NUMBER` | The actual number, digits only, for `wa.me` links. |
| `SESSION_SECRET` | Signs the storefront session cookie. |
| `DATABASE_URL` | `postgresql+psycopg2://whatsapp:…@localhost:5432/whatsapp` |
| `CHROMA_HOST` / `CHROMA_PORT` | `localhost` / `8001` (container 8000 → host 8001). |
| `EMBEDDING_PROVIDER` | `local` (default) or `openai`. |
| `OPENAI_API_KEY` | Optional. Only for conversational phrasing and openai embeddings. |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Not yet set. |

`.env` is gitignored and has never been committed.

# Appendix B — Commands

```bash
# Ingest the catalog (re-run after editing products.json or switching provider)
backend/.venv/Scripts/python scripts/ingest_catalog.py

# Regenerate placeholder images
backend/.venv/Scripts/python scripts/generate_placeholders.py

# Account linking end-to-end, including the merge case (18 assertions)
backend/.venv/Scripts/python scripts/test_linking.py

# Drive the bot without ngrok or a phone
backend/.venv/Scripts/python scripts/simulate_webhook.py --message "saree for office"

# Inspect the database
docker compose exec postgres psql -U whatsapp -d whatsapp -c "\d users"
```

# Appendix C — Database schema

```sql
users        user_id, whatsapp_number (unique, nullable), email (unique, nullable),
             password_hash, display_name, created_at, last_active_at
             CHECK (email IS NOT NULL OR whatsapp_number IS NOT NULL)

sessions     session_id, user_id → users, last_query, last_products_shown (JSONB),
             selected_product (JSONB), status, updated_at
             UNIQUE (user_id) WHERE status = 'active'

orders       order_id, user_id → users, channel (bot|web), product_id, product_name,
             price_inr NUMERIC(10,2), razorpay_order_id (unique), razorpay_payment_id,
             status (created|captured|failed), created_at, captured_at

link_tokens  token_id, token (unique), user_id → users, created_at, expires_at, used_at
```

# Appendix D — Repository layout

```
backend/
  main.py            app assembly, session middleware, static mount, lifespan
  models.py          SQLModel tables
  db.py              lazy engine, transactional session_scope
  repository.py      all CRUD; link token validation and merge
  auth.py            bcrypt, cookie session, current_user
  catalog.py         products.json accessor
  selection.py       follow-up parsing ("2", "the second one")
  whatsapp.py        Graph API send, with actionable error messages
  bot/chat.py        retrieval, gender filter, LLM phrasing   [Dev B]
  routers/           webhook.py · store.py · api.py
  templates/         base, index, product, login, signup, account
  static/products/   32 generated SVG placeholders
common/
  schemas.py         ProductMatch — the shared contract
  embeddings.py      provider switch, used by ingest AND query
data/products.json   32 products × 32 fields
scripts/             ingest_catalog · generate_placeholders · simulate_webhook · test_linking
docker-compose.yml   Postgres 15 + ChromaDB, persistent volumes
```

Roughly 3,700 lines across 12 commits.
