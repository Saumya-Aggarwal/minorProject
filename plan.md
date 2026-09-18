# E-commerce store + WhatsApp bot: shared cart and checkout

## Context

The store and the bot currently share identity but nothing else. A customer can
link their account and the bot will recognise them, but the site has no cart —
"Buy now" writes a single order row instantly — and the bot's `BUY` does the same.
Neither takes payment.

The goal is that both channels become two front doors to **one shopping session**:
add items in chat, see them on the website, check out from either side, and get the
confirmation in WhatsApp.

**Deadline 23 September**, roughly 4–5 working days, with Razorpay still unbuilt.
So this plan is split: a build slice that ships, and a documented roadmap that does
not. Both are needed — the roadmap is what answers "how would this scale?" in the viva.

---

## The payment question, answered first

**An agent cannot authorise a payment on the user's behalf.** From 1 April 2026 the
RBI mandates two-factor authentication on every digital payment in India; UPI PIN
entry must occur in a secure isolated environment (the user's own UPI app) and
merchants must integrate with compliant flows rather than around them.

This is a regulatory boundary, not an engineering one. Storing a PIN or card to
auto-pay would be building payment fraud infrastructure. Not doing it.

What the agent *does* do — everything except the tap:

```
 bot: builds cart, totals it, creates the Razorpay order,
      generates and sends the payment link, polls nothing,
      receives the webhook, records the order, confirms in chat
 human: taps the link, enters UPI PIN                     ← the only human step
```

**The legitimate autonomous version is UPI Autopay** (Phase 2, documented below):
the customer authorises a mandate once, with full AFA, and the merchant may then
debit within that limit without per-transaction approval. Razorpay supports it
today. That is the honest answer to "can the bot just pay for me" — yes, once,
with consent, within a ceiling the customer sets.

---

## Architecture: one spine, two front doors

```
   WhatsApp                                       Browser
       │                                             │
       │  "add it to my cart"                        │  [Add to cart]
       ▼                                             ▼
  routers/webhook.py                          routers/store.py
       └──────────────┬──────────────────────────────┘
                      ▼
            repository.py  (cart + order CRUD)
                      │
       ┌──────────────┴───────────────┐
       ▼                              ▼
  cart_items                     orders / order_items
  (user_id keyed —               (header + lines,
   the shared cart)               one Razorpay order)
                      │
                      ▼
              payments.py → Razorpay
                      │
          POST /razorpay/webhook (signature verified)
                      │
       ┌──────────────┴───────────────┐
       ▼                              ▼
  order marked captured        WhatsApp confirmation
  cart cleared                 (both channels)
```

**The cart is keyed by `user_id`, which is what makes it shared.** No new identity
mechanism is needed — `users` is already one row per person across both channels,
and `consume_link_token()` already merges bot and web identities. The shared cart
falls out of that for free. This is the payoff of the single-`users`-table decision.

---

# SHIP BY SEP 23

## 1. Schema — `backend/models.py`

Tables are disposable, so drop and recreate via `init_db()` as before.

**New `cart_items`:** `cart_item_id`, `user_id` → users, `product_id`,
`product_name`, `price_inr NUMERIC(10,2)`, `size`, `quantity`, `added_at`.
Unique on `(user_id, product_id, size)` so adding the same item twice increments
quantity instead of duplicating a row.

**Split `orders` into header + lines.** Today `orders` is one row per product,
with a UNIQUE `razorpay_order_id`. A cart checkout creates one Razorpay order for
several products, which that constraint forbids. So:

- `orders` becomes the header: `order_id`, `user_id`, `channel`, `total_inr`,
  `razorpay_order_id` (unique), `razorpay_payment_id`, `status`, `created_at`,
  `captured_at`
- new `order_items`: `order_item_id`, `order_id` → orders, `product_id`,
  `product_name`, `price_inr`, `size`, `quantity`

Price and name stay snapshotted on the line, for the reason already documented:
an order must record what was actually paid, not follow later catalog edits.

**Callers to update** (all in `backend/repository.py` unless noted):
`create_order()` → `create_order_from_cart(user_id, channel)`;
`get_order_history()` to join lines; `_place_order()` in
[routers/webhook.py](../backend/routers/webhook.py); `/buy` in
[routers/store.py](../backend/routers/store.py).

`mark_order_captured()` needs no change — it is already idempotent and keyed on
`razorpay_order_id`, which is exactly what the Razorpay webhook provides.

## 2. Cart — both channels

**Repository** (new functions, same `session_scope()` pattern):
`add_to_cart(user_id, product, size, qty)`, `get_cart(user_id)`,
`update_cart_item(...)`, `remove_from_cart(...)`, `clear_cart(user_id)`.

**Web** — `routers/store.py` + `routers/api.py`:
`POST /cart/add/{product_id}`, `GET /cart`, `POST /cart/update`,
`POST /cart/remove`, and `/api/cart` mirroring all of it. New `templates/cart.html`.
`/product/{id}` gains a size selector and "Add to cart" beside "Buy now".

**Bot** — extend the follow-up handling in
[routers/webhook.py](../backend/routers/webhook.py), reusing
[selection.py](../backend/selection.py)'s whole-message matching discipline so a
command never swallows a search:

| Message | Action |
|---|---|
| `ADD` after selecting an item | add `sessions.selected_product` to cart |
| `CART` | list cart with line totals and grand total |
| `REMOVE 2` | drop that line |
| `CHECKOUT` / `BUY ALL` | create order from cart, send payment link |

Cart requires a `user_id`, which the bot always has (created on first contact) and
the web has after login. Anonymous web carts are out of scope — an unlinked
anonymous cart has nowhere to merge to.

## 3. Razorpay — `backend/payments.py` (new)

Thin wrapper, no business logic: `create_razorpay_order(amount_inr, receipt, notes)`,
`create_payment_link(order, customer)`, `verify_webhook_signature(body, signature)`.

`backend/routers/payments.py` (new) — `POST /razorpay/webhook`:

- **Verify the signature before anything else.** HMAC-SHA256 of the *raw* body
  with `RAZORPAY_WEBHOOK_SECRET`, compared to the `X-Razorpay-Signature` header
  using `hmac.compare_digest`. Read the raw body, not the parsed JSON — re-serialising
  changes the bytes and the signature will never match.
- On `payment.captured` → `mark_order_captured()` → clear the cart → send the
  WhatsApp confirmation. It returns `True` only on first capture, so a redelivered
  webhook cannot double-confirm.
- On `payment.failed` → `mark_order_failed()` → tell the customer in chat.
- **Always return 200**, same discipline as the Meta webhook and for the same
  reason (Part 4.1 of the project report).

**The Pay Now message** uses the interactive `cta_url` payload from the sprint
plan: header with the order summary, body with the total, button opening the
Razorpay payment link. Native in-chat UPI needs WhatsApp Payments approval that
will not arrive in time; `cta_url` + payment link is one tap from chat to UPI PIN.

## 4. WhatsApp as the notification channel

Orders placed **on the website** also confirm in WhatsApp, when the customer has
linked. The capture handler already knows `user_id`; if `users.whatsapp_number` is
set, send the confirmation. This is what makes the two channels read as one system,
and it costs almost nothing once the capture path exists.

## 5. Cheap wins (half a day together, done after Razorpay works)

- **Bot links back to product pages.** Append `PUBLIC_BASE_URL/product/{id}` to
  each recommendation. New env var, set to the ngrok URL.
- **"Recommended for you"** on `/` and `/account`: run the customer's last purchase
  description through the existing retrieval in
  [bot/chat.py](../backend/bot/chat.py), exclude what they already own. Proves the
  RAG engine is shared infrastructure rather than a chatbot trick — the strongest
  viva point available for the effort.
- **Category / gender / price filters** on the grid, straight from
  [catalog.py](../backend/catalog.py) — no vector search needed, plain dict filtering.
- **Order tracking page** `/orders/{id}` with line items, status and delivery
  estimate from `delivery_days`.

---

## Sequencing

| Day | Work |
|---|---|
| **Sep 19** | Schema (cart_items, orders/order_items split), repository CRUD, web cart pages, bot cart commands |
| **Sep 20** | Razorpay: order creation, payment link, `cta_url` message, webhook + signature, capture path, WhatsApp confirmation both channels |
| **Sep 21** | Cheap wins: deep links, recommendations, filters, order tracking |
| **Sep 22** | Hardening: payment failure, empty cart, stale link, out-of-stock. Seed demo data |
| **Sep 23** | Record backup video, rehearse twice |

**Razorpay is the risk.** If Sep 20 overruns, Sep 21's items are the buffer — cut
in this order: filters, order tracking, deep links. **Keep "Recommended for you"**;
it carries more marks per hour than the rest combined.

**Hard prerequisite:** `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `.env` before
Sep 20 starts. Test mode is sufficient and appropriate — it demonstrates the full
flow including failure cases, which live mode makes awkward.

---

# DOCUMENTED, NOT BUILT — Phase 2

State these as deliberate scope decisions, not omissions.

**UPI Autopay agent payments.** The customer authorises a mandate once with a
ceiling (say ₹5,000/month); the assistant then completes purchases within it
without a per-transaction tap. This is the compliant form of "the bot pays for me",
and the natural next step for the agentic angle.

**Shared cart beyond identity.** Real inventory reservation, stock decrement on
capture, and release on abandonment. Currently `stock` in `products.json` is
displayed but never decremented.

**Size selection in chat.** The web cart takes a size; the bot currently defaults.
Needs a follow-up prompt ("which size — M, L or XL?") and another conversation state.

**Next.js storefront** against the existing `/api/*`. The JSON API was built for
exactly this; the Jinja templates are the disposable first client.

**Alembic migrations.** Schema changes currently drop and recreate. Fine while the
data is disposable, not once it holds real orders.

**Order fulfilment** — shipping, tracking numbers, returns. Out of scope entirely.

---

## Verification

1. **Schema**: `docker compose exec postgres psql -U whatsapp -d whatsapp -c "\d cart_items"`
   and `\d order_items`; confirm the unique constraint on `(user_id, product_id, size)`.
2. **Shared cart, the headline test**: add an item in chat (`ADD`), refresh `/cart`
   in the browser, confirm it is there. Add a second on the website, send `CART`
   to the bot, confirm both appear with a correct total.
3. **Checkout from chat**: `CHECKOUT` → Pay Now message arrives → tap → Razorpay
   test page → pay with test card `4111 1111 1111 1111` → confirmation in WhatsApp
   → order visible in `/account` with both line items.
4. **Idempotency**: replay the capture webhook (Razorpay dashboard has a redeliver
   button) and confirm exactly one confirmation message and no duplicate order.
5. **Signature rejection**: `curl` the webhook with a wrong signature → refused,
   nothing written.
6. **Failure path**: pay with Razorpay's failure test card → order marked `failed`,
   customer told in chat, cart *not* cleared.
7. **Regression**: `scripts/test_linking.py` still passes 18/18 after the schema
   split, and `scripts/simulate_webhook.py` still drives a conversation.
8. **Web order → WhatsApp**: buy on the site as a linked customer, confirm the
   notification arrives in chat.

Extend `scripts/test_linking.py` (or add `scripts/test_cart.py` in the same style)
to cover the cart merge case: items added by the bot *before* linking should follow
the customer into their web account, exactly as orders and sessions already do.
