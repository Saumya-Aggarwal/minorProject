# Work split — 19 to 23 September

Two parallel workstreams, one per developer. Architecture and rationale for the
remaining work are in [plan.md](plan.md); this file says **who builds what, which
files each person owns, and the few interfaces that cross between them.**

| | Owner | Theme |
|---|---|---|
| **Part A** | Dev A — ________ | **Buying**: identity, cart, orders, payments, WhatsApp transport |
| **Part B** | Dev B — ________ | **Finding**: retrieval, LLM replies, browsing, search, recommendations |

Fill in the names. The split follows the ownership already in CLAUDE.md (Dev A:
FastAPI/WhatsApp/Razorpay, Dev B: ChromaDB/RAG), with one deliberate change:
**the Postgres schema moves to Dev A**, because every remaining schema change
(cart, order lines) is driven by checkout. One owner for `models.py` and
`repository.py` removes the most likely merge conflict.

"Finding vs buying" is also a clean way to present the architecture to the panel.

---

## 0. Before anyone writes code — Sep 19 morning, 30 min together

1. **Clone and branch.** Code is on `github.com/Saumya-Aggarwal/minorProject`,
   and `main` is up to date. Add Dev B as a collaborator. Each person works on
   their own branch (`a/cart`, `b/llm`, …) and merges to `main` at least once a
   day, after the checks in section 6 pass.

2. **Dev B setup** (Windows, PowerShell):
   ```powershell
   git clone https://github.com/Saumya-Aggarwal/minorProject.git
   cd minorProject\backend
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env        # fill in values, see point 3
   cd ..
   docker compose up -d
   backend\.venv\Scripts\python scripts\ingest_catalog.py   # first run downloads a ~80MB model
   cd backend; uvicorn main:app --reload --port 8000
   ```
   In another terminal, test without WhatsApp or a phone:
   ```powershell
   backend\.venv\Scripts\python scripts\simulate_webhook.py --message "saree for office"
   ```

3. **Secrets.** Dev B does **not** need the WhatsApp token — `simulate_webhook.py`
   sends `X-Local-Test: true`, which skips sending. Dev B needs only the database,
   Chroma and their **own** Groq key (separate keys also double the free quota
   during development). Never share keys through git or chat.

4. **Only Dev A's machine talks to Meta.** The webhook URL registered in the Meta
   dashboard points at one ngrok tunnel. Live WhatsApp testing happens there;
   Dev B tests with `simulate_webhook.py` and the browser.

5. **Agree the contracts in section 2** before splitting up.

---

## 1. File ownership

**Only edit files you own.** Need a change in the other person's file? Ask, or
open a small PR for them to review.

| Dev A owns | Dev B owns |
|---|---|
| `backend/models.py`, `db.py`, `repository.py` | `backend/bot/chat.py` |
| `backend/routers/webhook.py`, `whatsapp.py`, `selection.py`, `auth.py` | `common/embeddings.py` |
| `backend/routers/store.py`, `api.py`, `payments.py`, `browse.py` | `scripts/ingest_catalog.py` |
| `backend/payments.py`, `checkout.py`, `shipping.py` | `scripts/eval_retrieval.py` **(new)** |
| `backend/search.py` (site search, similar, recommended) | |
| `backend/catalog.py` (filters, sort, facets) | |
| **all templates**, `templating.py`, `backend/static/`, `backend/tailwind/` | |
| `data/products.json`, `data/photo_choices.json`, `scripts/fetch_photos.py` | |
| `scripts/test_*.py`, `scripts/test_live_js.mjs` | |

> **Changed on 21 Sep — the website moved to Dev A.** The storefront redesign
> (photos, checkout with address, search, filters) touched the home, product and
> search pages, which were Dev B's. Dev B had not started them, so the whole
> website UI moved to Dev A rather than split one page across two people.
> Dev B keeps everything the bot says: `bot/chat.py`, embeddings, ingestion,
> the LLM and retrieval tuning. B5 and B6 below are therefore already done (by
> A, in `search.py` and `catalog.py`); B7 and the rest of Part B are unchanged.
> **Catalogue edits** (`data/products.json`) are A's, but tell B, because the
> Chroma index must be rebuilt with `scripts/ingest_catalog.py` afterwards.

**Shared — append-only, small commits, pull before editing:**
`main.py` (router registration), `templates/base.html` (A adds a Cart link,
B adds a search box), `requirements.txt`, `backend/.env.example`, `CLAUDE.md`,
`common/schemas.py` (changes need both).

---

## 2. Contracts — the only things that cross the line

Change any of these → message the other person **before** pushing.

### C1 · The bot reply (B produces, A consumes)

```python
get_product_recommendations(query: str, top_k: int = 3,
                            user_context: str | None = None) -> tuple[str, list[dict]]
```

- The reply is an **LLM-written intro (1–2 sentences) followed by a numbered list
  built by code**, not by the LLM.
- **Invariant: the numbered list is in exactly the same order as the returned
  matches.** A's item selection maps "2" to `matches[1]`. If the order differs,
  the customer buys a product they did not pick. This is the single most
  important rule in this file.
- The reply ends after the list. **A appends the command footer** ("Reply 1–3 to
  choose · CART to see your cart"), because A owns the commands. B removes the
  current "Reply with an item number…" line from `_format_matches()`. If A merges
  first, the footer shows twice for a day — harmless.

### C2 · Purchase history (A produces, B consumes)

```python
repository.get_purchased_product_ids(user_id: int) -> list[str]
```

Distinct product ids, most recent first, from orders with status `created` or
`captured` (nothing is `captured` until Razorpay is live). A narrow function on
purpose: the order tables are being restructured, and B should not depend on
their shape. **A ships this on Sep 19**, even as a stub over the current schema —
B needs it on Sep 20.

### C3 · Similar products — done by A, no longer a contract

`search.similar_products(product_id, k=4)` and `search.recommended_for(ids, k=4)`
read the same Chroma collection the bot uses, through `common/embeddings.py`.
They only read the index, so B can change how it is built as long as the
collection name ("products") and the metadata keys (`gender`, `category`) stay.

### C4 · Image description — stretch only (B produces, A calls)

```python
describe_image(image_bytes: bytes, mime_type: str) -> str
```

A downloads the WhatsApp media; B turns it into a text description that goes
through normal retrieval. Only if everything else is done.

### C5 · Cart form (A implements, B's page includes it)

`POST /cart/add/{product_id}` with form fields `size` and `quantity`. Lives in
A's `buy_box.html`, so in practice only A touches it.

---

## PART A — Buying (Dev A)

This is the critical path of the whole project. **Razorpay (A3) must work by
Sep 20 evening.**

**A6 · Stable public URL** — Sep 19, 10 min, do first
Use ngrok's free static domain so the URL stops changing on every restart. This
also ends the "re-point the Meta webhook after each restart" chore. Put it in
`.env` as `PUBLIC_BASE_URL` and tell B, who needs it for B7.

**A1 · Schema split** — Sep 19
- `orders` becomes a header (`total_inr`, Razorpay ids, status); new
  `order_items` for the lines; new `cart_items`, unique on
  `(user_id, product_id, size)`. Details in plan.md §1.
- Update the callers: `create_order`, `get_order_history`, `_place_order`,
  `/buy`, `_build_user_context`.
- Add C2 `get_purchased_product_ids()` and push it the same day.
- Account linking: `consume_link_token()` must also re-point `cart_items`, or a
  customer's chat cart disappears when they link.
- **Done when:** `test_linking.py` passes 18/18 and a new `test_cart.py` passes,
  including cart items following the customer through account linking.

**A2 · Cart on both channels** — Sep 19–20
- Repository: `add_to_cart`, `get_cart`, `update_cart_item`, `remove_from_cart`,
  `clear_cart`.
- Bot: `ADD` (after selecting an item), `CART`, `REMOVE 2`, `CHECKOUT` /
  `BUY ALL`. Whole-message matching, like `selection.py`, so a command never
  swallows a search.
- Web: `/cart`, `/cart/add`, `/cart/update`, `/cart/remove`, `/api/cart`,
  `templates/cart.html`, `partials/buy_box.html`.
- Append the command footer to bot replies (C1).
- **Done when:** an item added in chat appears at `/cart` in the browser, and
  the other way round.

**A3 · Razorpay** — Sep 20
- `payments.py`: create order, create payment link, verify signature.
- `routers/payments.py`: `POST /razorpay/webhook`. **Verify the HMAC over the raw
  request body**, not re-serialised JSON — re-serialising changes the bytes and
  the signature never matches. Always return 200.
- `payment.captured` → `mark_order_captured()` (already idempotent) → clear the
  cart → WhatsApp confirmation. `payment.failed` → mark failed, tell the
  customer, keep the cart.
- Pay Now as an interactive `cta_url` message.
- New env vars: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`.
- **Done when:** `CHECKOUT` in chat → Pay Now → test card `4111 1111 1111 1111`
  → confirmation in WhatsApp → order in `/account`. Redelivering the webhook from
  the Razorpay dashboard sends no second confirmation.

**A4 · WhatsApp notifications for web orders** — Sep 21
Orders placed on the site confirm in WhatsApp when the customer has linked.

**A5 · Order tracking** — Sep 21
`/orders/{id}` with lines, status and delivery estimate; an order list in `/account`.

**If behind, cut in this order:** A5, then A4. Never cut A1–A3.

---

## PART B — Finding (Dev B)

**Context, if you haven't followed the build:** the bot already retrieves
products from Chroma using local MiniLM embeddings (`common/embeddings.py`, no
API key needed), with a gender metadata filter in `bot/chat.py`. Replies are
currently a plain numbered list because no LLM is configured. Known retrieval
misses: "jacket to wear over my kurta" ranks a kurta first; "me and my wife"
filters to women only; budgets like "under 3000" are **ignored entirely**. Full
background: `docs/PROJECT_REPORT.md`, Parts 3.3 and 4.4.

**B3 · Evaluation script first** — Sep 19 morning
`scripts/eval_retrieval.py`: about 20 queries, each with what a correct answer
must satisfy (category, gender, price cap), printing a hit rate. **Run it before
changing anything** to record a baseline. Without a baseline you cannot show an
improvement, and "retrieval accuracy went from X to Y" is strong report material.
Include the known misses above.

**B1 · LLM replies on Groq** — Sep 19
- `bot/chat.py`, provider-agnostic through `LLM_API_KEY`, `LLM_BASE_URL`
  (`https://api.groq.com/openai/v1`), `LLM_MODEL` (`openai/gpt-oss-20b`, with
  `openai/gpt-oss-120b` as the fallback) and `LLM_REASONING_EFFORT` (`low`).
- The LLM writes **only the 1–2 sentence intro**. Code builds the numbered list
  with names and prices (C1), so prices cannot be hallucinated.
- `max_tokens` around 1000 (reasoning tokens likely count against it), a timeout
  of about 8 seconds, `max_retries=1`. Keep the existing fallback to the plain list.
- Add the new variables to `.env.example`.
- **Done when:** five queries return intro + correct list, removing the key falls
  back cleanly, and response time is logged.

**B2 · Retrieval filters** — Sep 19–20
- **Budget**: "under 3000", "below 2k", "budget 15000" → `price <= N`. Decide how
  to treat "around N" (for example up to 1.15 × N) and write down why.
- **Category**: when the query names one ("jacket", "saree", "dupatta"), filter or
  boost by it. This should fix the jacket case.
- **Self plus partner** ("me and my wife") → no gender filter.
- Combine filters with Chroma's `$and`.
- **Done when:** the eval hit rate beats the baseline and no previously passing
  query now fails.

**B4 · `user_context`** — Sep 20
The agreed contract addition. It goes into the intro prompt only ("pairs well
with the kurta you bought"). `webhook.py` already passes it as soon as the
parameter exists.

**B5 · Recommended for you** — Sep 20 — **done by A (see section 1)**
C3 `get_similar_products()` plus `partials/recommendations.html`, using C2 from
A. On the home page for signed-in customers, falling back to top-rated products
for everyone else. The strongest viva point per hour of work: it shows the
retrieval engine is shared between the site and the bot.

**B6 · Browse and search** — Sep 21 — **done by A (see section 1)**
`routers/browse.py`: category/gender/price filters on the grid (plain filtering
in `catalog.py`, no vectors needed) and `/search?q=` using the **same semantic
retrieval as the bot**, with `/api/search` as its JSON twin.

**B7 · Links from chat to product pages** — Sep 21
Each item in the bot's list gets `PUBLIC_BASE_URL/product/{id}` (from A6).

**B8 · Report** — Sep 21
Update `docs/PROJECT_REPORT.md` Part 3.3 with the eval numbers and the new filters.

**Stretch B9 · Photo search** — only if everything is green on Sep 21 evening
C4 using `nemotron-3-nano-omni-30b-a3b-reasoning` on NVIDIA's free endpoint.
Demo with product photos, not photos of people — NVIDIA's terms say no faces.

**If behind, cut in this order:** B9, then the B6 search box, then the B6 filters,
then B7. Keep B1, B2, B3 and B5.

---

## 5. Timeline

| Day | Dev A | Dev B | Together |
|---|---|---|---|
| **Sep 19** | A6, A1, start A2 | B3 baseline, B1, start B2 | Morning: section 0. Evening: A pushes C2 |
| **Sep 20** | Finish A2, **A3 Razorpay** | Finish B2, B4, B5 | Evening: merge both, run section 6 checks |
| **Sep 21** | A4, A5 | B6, B7, B8 | Evening: full run on a real phone (A's machine) |
| **Sep 22** | Hardening: payment failure, empty cart, stale link | Hardening; B9 only if green | Pair on the demo script, seed demo accounts |
| **Sep 23** | — | — | **Record the backup video**, rehearse twice |

Dev B's work is lighter and off the critical path. If B finishes early, the most
useful thing is testing A's checkout flow from a second phone.

---

## 6. Checks before merging to main

```powershell
backend\.venv\Scripts\python scripts\test_linking.py      # 18/18
backend\.venv\Scripts\python scripts\test_cart.py         # from Sep 19 (A)
backend\.venv\Scripts\python scripts\eval_retrieval.py    # at least the last merged result (B)
backend\.venv\Scripts\python scripts\simulate_webhook.py --message "sherwani for my wedding"
```

After any merge that touches both sides, run one full conversation:
search → `2` → `ADD` → `CART` → `CHECKOUT`.

## 7. Rules that prevent lost days

- Pull before you start, push small commits, merge daily. A three-day branch is
  a three-day merge conflict.
- Schema changes are Dev A's. Need a column? Ask.
- `.env` is never committed. When you add a variable, add it to `.env.example`
  in the same commit.
- If a check in section 6 fails after a merge, fix it before starting anything new.
