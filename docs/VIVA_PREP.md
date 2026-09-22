# Viva Preparation — Munim: Context-Aware WhatsApp Shopping Assistant

**Minor Project, 7th Semester · Read this before the presentation.**

Every number, threshold and file name below was read out of the code on 23 September 2026.
Where a document in the repo disagrees with the code, the code wins and the difference is
flagged, so you are never caught defending a wrong number.

**How to use this document.** Each topic has three parts: *How it works* (the mechanics),
*Why it is built this way* (the alternative that was rejected), and *Five questions the
panel may ask*. Inside each set of five, the easy question is first and the uncomfortable
one is last. If you have only twenty minutes, read Section 0 and the last question of every
topic.

---

## 0. Cheat sheet

### The project in one minute

"A customer messages our WhatsApp number in plain language — *something for my sister's
mehendi, nothing too heavy, under 3000*. The message hits a FastAPI webhook. We embed the
request, rank all 84 products in ChromaDB by meaning, then apply the hard constraints —
gender, garment type, budget, stock — in Python, because embeddings rank but they do not
constrain. The customer gets three photos with prices, picks one by typing *2*, adds it to
the cart with a size, and pays through a Razorpay link inside the chat. The same cart and
the same order history are on our website, because both channels read one `users` table,
and the two identities are bound by a single-use code the customer sends from their own
phone. The language model chooses what to do, but every product, price and total on the
screen is rendered by code — a bot that invents a price is worse than no bot."

### Numbers to quote

| Fact | Value |
|---|---|
| Catalogue | 84 products, 33 fields each, 23 categories (45 women's, 39 men's), ₹699–₹24,999 |
| Occasion vocabulary | 33 distinct tags, 274 tag assignments, ≈3.3 per product |
| Embeddings | all-MiniLM-L6-v2, **384 dimensions**, local CPU, free (OpenAI 1536-dim optional) |
| Language model | Groq `openai/gpt-oss-20b`, fallbacks `gpt-oss-120b`, `qwen3.8-27b` |
| Retrieval evaluation | **46 / 46 cases pass**, and 10/10 owner ratings hold (every product returned must satisfy the query) |
| Automated checks | **434, all green on 23 Sep**: cart 126, assistant 74, payments 61, store 36, training 28, live 20, linking 18, live.js 15, plus 46 retrieval cases and 10 owner-rating regressions |
| Code size | ~10,465 lines of Python, 16 Jinja templates, 41 commits over 18–23 Sep |
| Distance cut-off | `MAX_DISTANCE = 1.55` (squared L2, range 0–4) |
| Question-similarity threshold for training | cosine ≥ **0.75** |
| Conversation memory | last 12 messages stored, **8** sent to the model, 350 chars each |
| Payment reconciler | every **5 s**, orders from the last **60 min**, batch of 10 |
| Live sync | poll `/api/live` every **2 s**, SHA-1 fingerprint truncated to 16 hex chars |

### Demo script (60 seconds, in this order)

1. `sherwani for my wedding under 10000` → three photo cards with prices.
2. `2` → that product is selected and described.
3. `ADD 42` → cart with the real total.
4. Show the website in a browser: **the same cart appears within two seconds**, no refresh.
5. `CHECKOUT` → Pay Now button → pay with test card `4111 1111 1111 1111`.
6. Payment confirmation arrives in the chat; the order page on the website flips to *paid*.

If Wi-Fi fails, say so and play the recording. Do not debug live.

### The ten hardest questions, with the one-line answer

1. **"Isn't this just ChatGPT with a WhatsApp number?"** — The model never states a fact.
   Products, prices, carts and totals are rendered by code from the database; the model
   only chooses which tool to call and writes the sentence around the list.
2. **"What is the novelty?"** — Two: grounding an LLM hard enough to be trusted with money
   (every rupee it writes must exist in the catalogue or a tool result), and bridging a
   browser identity to a phone number that carries no session.
3. **"Why not just use SQL/keyword search?"** — Because *"nothing too heavy for a mehendi"*
   has no keyword. We evaluate this: 46 phrasings, and the old keyword filters scored 8/10
   on an easier 10-case set, returning **nothing at all** for two of them.
4. **"How do you know the retrieval is good?"** — `scripts/eval_retrieval.py`: a case passes
   only when **every** returned product satisfies the constraint. Currently 46/46.
5. **"What stops the bot inventing a discount?"** — Amount verification: any ₹ figure in the
   model's own words must appear in the catalogue, the customer's message, or a tool result;
   otherwise the model is told to retry once and then the sentence is deleted.
6. **"Could someone else's phone number claim my account?"** — Only with a single-use code,
   valid 10 minutes, minted for a signed-in browser session. Same trust model as an email
   confirmation link.
7. **"What if Razorpay's webhook never arrives?"** — Three independent confirmation paths,
   any one sufficient: browser verify, signed webhook, and a background reconciler that
   polls Razorpay every 5 seconds. Capture is row-locked and idempotent, so exactly one
   confirmation is sent.
8. **"Is the stock real?"** — No. `in_stock` is displayed but never decremented. It is
   documented as Phase 2, because real reservation needs locking and expiry, not a boolean.
9. **"What happens when the LLM is down or rate-limited?"** — It degrades in three steps:
   next model, short wait, then a plain retrieval reply with no model at all. The shop keeps
   selling.
10. **"What is the weakest part of this project?"** — Deployment. It runs behind ngrok on a
    Meta test number with a 24-hour messaging window and no schema migrations. The logic is
    tested; the operations are not production-grade, and we can list exactly what that needs.

---

## 1. What the project is

### How it works

The system has **two front doors and one spine**. The front doors are a WhatsApp number
(Meta Cloud API) and a web storefront (server-rendered Jinja pages). The spine is one
FastAPI application with one PostgreSQL database, one product catalogue and one vector
index. Because both doors read the same `users`, `cart_items` and `orders` tables, a
conversation that starts in chat can finish in a browser, and the other way round.

The domain is Indian ethnic wear: 84 products across 23 categories, described by occasion
(haldi, mehendi, sangeet, reception, garba, office), fabric, colour, fit and cut. That
vocabulary is exactly what meaning-based retrieval is good at and keyword search is bad at.

Scope was deliberately bounded: one store, English and simple Hinglish, a Meta test number,
Razorpay in test mode. Everything outside that is listed in Section 12 as future work, with
the reason it was excluded rather than a promise that it is "coming soon".

### Why it is built this way

The obvious alternative was a menu-driven bot — *Reply 1 for sarees, 2 for kurtas*. That is
cheaper to build, and it solves nothing: the customer still has to translate what they want
into our categories. The entire point is to accept the sentence the customer would say to a
shopkeeper. The second alternative was to build only a website with a chat widget, which
loses the one thing that makes this useful in India: the customer is already in WhatsApp,
and there is no app to install, no password, and no form.

### Five questions the panel may ask

**Q1. Describe your project in two sentences.**
A WhatsApp shopping assistant for an ethnic-wear store: the customer describes what they
need in plain language and gets real products, with photos and prices, retrieved from the
store's catalogue by meaning rather than keywords. It shares one cart, one account and one
order history with a companion website, and the customer can pay without leaving the chat.

**Q2. Who is the user, and what problem do they actually have?**
Two users. The customer, who does not want to install an app, browse a grid of 84 products,
or repeat what they already said. The store owner, who today answers the same product
questions by hand on WhatsApp, sends photos manually and loses sales outside business hours.
Our assistant does that work and keeps the answers grounded in the real catalogue.

**Q3. Why Indian ethnic wear and not electronics or groceries?**
Because the request is expressed in meaning, not specifications. Nobody says "something
light for my sister's mehendi" about a laptop; they say "16 GB RAM under 60k", which SQL
handles perfectly. Ethnic wear is described by occasion, fabric, colour and fit — the
vocabulary that embeddings capture and filters cannot.

**Q4. What is in scope and what is not?**
In scope: one store, 84 products, English and light Hinglish text, WhatsApp Cloud API on a
test number, Razorpay test mode, the full path from a question to a paid order, and an admin
page where the owner teaches the assistant. Not in scope: multiple stores on one deployment,
Shopify or WooCommerce sync, voice notes, image search, courier tracking, and real inventory
reservation. Each exclusion is a stated design decision in `plan.md`, not an oversight.

**Q5. This looks like an integration of existing services. What did you actually build?**
The integrations are the easy half. What we built is the part that makes them safe together:
a retrieval engine that applies hard constraints after semantic ranking, a tool-calling
assistant with code-enforced trust boundaries, an identity bridge between a browser session
and a phone number, an exactly-once payment confirmation across three racing paths, and a
feedback loop that lets the owner correct the ranking without retraining anything. That is
about 10,500 lines of Python, of which roughly 2,000 are tests.

---

## 2. The end-to-end flow

### How it works

Follow one message the whole way.

**1. Delivery.** Meta POSTs to `/webhook` (`backend/routers/webhook.py`). We check
`hub.verify_token` on the initial GET handshake and echo the challenge as plain text — JSON
fails Meta's check.

**2. Deduplication.** Meta re-delivers when our acknowledgement is slow or lost. An
in-process `OrderedDict` of the last **500** message ids drops repeats, so a redelivered
message can never add to the cart twice.

**3. Acknowledge first, work later.** The handler adds `_answer_and_send` as a background
task and returns HTTP 200 immediately. Every exception path also returns 200, because a 500
tells Meta the delivery failed and it retries into the same failure — that is the retry
storm we hit on day one.

**4. Read receipt and typing indicator** are sent before the work starts, so the customer
sees the bot is alive during the two or three seconds that follow.

**5. Routing, in this order** (`_handle_text`): an account-link code; then exact commands —
a number, `ADD`, `CART`, `REMOVE n`, `SIZE n x`, `CHECKOUT`, `BUY`, `ORDERS`, `HELP`, a
product code like `EW006`; then greetings; then questions about the saved address; then
re-order requests; then cart/total/order lookups. Only what survives all of that reaches the
language model. This ordering is the reason "2" never becomes a search and "hi" never
returns three random kurtas.

**6. The assistant** (`bot/assistant.py`) gets the last 8 messages, the customer's real cart,
their name and purchase history, and 8 tools. It asks a clarifying question when the request
is vague and calls `search_products` when it is not.

**7. Retrieval** (`bot/retrieval.py`) embeds the query, ranks the whole catalogue in
ChromaDB, then filters in Python: stock, personal hides, gender, category, budget. If
nothing passes, it relaxes budget, then category, and says so in the reply.

**8. The reply is assembled by code.** `send_reply` receives only product *ids*; the
numbered list, the captions, the prices and the photos are built from the catalogue. Each
product is sent as its own WhatsApp image message with two buttons — *Choose* and *Not for
me* — preceded by the model's one-sentence intro and followed by the command footer.

**9. Selection and cart.** `2` resolves against `last_products_shown` in the session row;
`ADD 42` adds the selected product in size 42 to `cart_items`, an atomic upsert so two taps
cannot race.

**10. Checkout.** `CHECKOUT` requires a size on every line and a delivery address, creates an
order with the address snapshotted onto it, creates a Razorpay order, and sends a Pay Now
button pointing at our own `/pay/{order_id}` page.

**11. Payment and confirmation.** The customer pays; the capture is recorded exactly once
under a row lock; the ordered quantities leave the cart; a confirmation goes back to
WhatsApp; and any open website tab re-renders within two seconds.

### Why it is built this way

The shape of this flow is dictated by one constraint: **Meta's webhook must be acknowledged
fast, and it retries anything that is not acknowledged.** So the slow work — embedding,
retrieval, an LLM round trip, image upload — cannot happen before the response. Everything
else follows: background tasks, deduplication by message id, and swallowing send failures
rather than raising, because one lost reply is better than an infinite retry loop.

### Five questions the panel may ask

**Q1. Walk me through what happens when I send "sherwani for my wedding".**
Meta posts it to our webhook; we ack 200 and answer in the background. It is not a command
or a greeting, so the assistant sees it, judges it clear enough to search, and calls
`search_products`. We embed the sentence, rank all 84 products, keep men's Sherwani that are
in stock, boost items tagged *wedding*, and take the top three. Code renders three photo
cards with the real prices and a footer telling you to reply with a number.

**Q2. Why return 200 before you have done the work?**
Because the webhook contract is an acknowledgement of *receipt*, not of processing. Our work
takes seconds — LLM plus retrieval plus image upload — and Meta would re-deliver the message
meanwhile. We answer asynchronously and dedupe by message id so a redelivery is harmless.

**Q3. What happens if two messages arrive at the same time?**
Each is its own background task. Shared state is protected where it matters: cart writes are
a single `INSERT ... ON CONFLICT DO UPDATE`, so simultaneous adds cannot violate the unique
constraint — `test_cart.py` fires eight concurrent adds to prove it — and payment capture
takes a row lock.

**Q4. Where exactly does the language model sit in that flow, and what can it not do?**
It sits at step 6, after all deterministic routing. It cannot name a price, print a product
list, act for another user, or add to the cart unless the customer's current message asks
for it and states a size they typed. It receives product ids and returns product ids; the
rendering is ours.

**Q5. If the panel asks you to break it — what is the most fragile step?**
The Meta transport. The test number can only message numbers on an allow-list, free-form
replies only work within 24 hours of the customer's last message, and the tunnel URL changes
on every ngrok restart. Those are deployment constraints, not logic bugs, and the reason we
rehearse with a recording ready.

---
## 3. Retrieval — how the RAG actually ranks

### How it works

**Ingestion** (`scripts/ingest_catalog.py`). For each product we build one document by
joining seven parts: the title; `Category: {gender} {category}, {subcategory}`; fabric,
colour and pattern; fit, sleeve and neckline; `Suitable for: {occasions}`; the description;
and the highlights. **Price and stock are deliberately not embedded** — they are filters,
not meaning, and bare numbers add noise to the vector. The document is embedded with
all-MiniLM-L6-v2 into a **384-dimensional** vector and upserted into the Chroma collection
`products`, together with flat metadata (id, name, category, gender, fabric, price, mrp,
occasion, sizes, in_stock, image_url).

**Query time** (`bot/retrieval.py`), in order:

1. **Parse the sentence into filters** — gender, categories, budget, occasion. The occasion
   is *soft*: it is appended to the text we embed, not used as a filter.
2. **Rank the whole catalogue.** One embedding, one Chroma query asking for as many results
   as there are products. Chroma's HNSW index is approximate: asked for all 84 items it
   still skipped EW080, so any catalogue id missing from the result is fetched by id and
   scored exactly against its stored vector with the same metric. Nothing is unfindable.
3. **Stock filter**, then remove anything this customer has personally hidden.
4. **Hard filters in Python**: gender must match exactly; category must be in the requested
   set; price within budget; and if no category was asked for, accessories — Footwear, Bags,
   Headwear — are suppressed, so "haldi outfit" never returns juttis.
5. **Distance cut-off**, `MAX_DISTANCE = 1.55` on squared L2 distance (range 0–4), applied
   **only when the query produced no filters at all**. This is what makes `asdfgh qwerty`
   return nothing instead of three random kurtas, while a constrained query like "kurta under
   2000" still returns the best available matches.
6. **Occasion boost**: a stable sort putting products tagged for the function first, so
   within each group the semantic order survives. Tag synonyms are expanded — mehndi→mehendi,
   garba/dandiya→garba+navratri, pheras→wedding+bridal, baraat→baraat+groom.
7. **Owner training re-rank**: net ✓/✕ ratings for questions similar to this one (cosine
   ≥ 0.75), clamped to ±1, applied as another stable sort.
8. **Couple interleave** when the query means two people ("me and my wife", "couple",
   "matching outfits"): results are zipped women-first, men-second.

**Relaxation.** If nothing passes, filters are dropped in a fixed order — first budget, then
budget *and* category — and `SearchResult.note()` says so honestly: *"No lehengas under
₹3,000 right now — these are the closest by price."* When only the budget was relaxed, the
results are re-sorted by distance from the intended spend, so a customer asking for ₹3,000
sees ₹3,400 before ₹12,000. **Gender is never relaxed.**

**Garment vocabulary.** 114 garment words map onto categories that actually exist, and some
are gender-dependent: "kurta" for a man means Kurta or Kurta Set, for a woman Kurti, Salwar
Suit or Co-ord Set. "Jacket" maps to Nehru Jacket and Waistcoat, because a category called
"Jacket" does not exist — that was a real miss. A garment after a possessive ("my kurta") or
after match/with/over is treated as **context, not the request**, but it still votes on
gender: "shoes to go with my sherwani" returns men's footwear.

**Budget parsing** handles the forms people type: "under 2k", "below rs 1000", "between 5000
and 10000", "around ₹3000" (widened by 1.15×, so ₹3,450). Numbers below 100 are never read
as budgets, so "size 42" is not a price.

**When Chroma is down**, `rank()` clears its cached handle so the next call reconnects, and
falls back to keyword overlap scoring with synthetic distances, so the shop and the website
search box still answer.

### Why it is built this way

The first version trusted embeddings to respect constraints. It ranked a women's kurta set
first for "groom outfit budget 12000". The team tested the obvious hypothesis — that price
and size text was polluting the vectors — by embedding both versions and comparing: 0.652
versus 0.590 on the same wrong products. **Cleaning the documents changed nothing; the
hypothesis was wrong.** The real lesson is in the report: *embeddings rank, they do not
constrain*. Anything that is a hard requirement belongs in a filter applied after ranking.

### Five questions the panel may ask

**Q1. What is RAG, in your system?**
Retrieval-Augmented Generation: instead of asking a language model what to recommend, we
retrieve real products from our own catalogue by meaning and give the model only those. It
writes one sentence around a list that code renders. The model cannot recommend a product
that does not exist, because it only ever handles ids we gave it.

**Q2. Why embeddings and not a SQL LIKE query?**
Because *"nothing too heavy for a mehendi"* contains no keyword present in the data. An
embedding puts that sentence near products whose descriptions talk about light georgette,
daytime functions and mehendi. We keep SQL-style constraints too — they run *after* the
ranking, in Python, where they are exact.

**Q3. Why MiniLM and 384 dimensions rather than OpenAI's model?**
Three reasons, in order: it costs nothing, it runs offline on CPU, and **it cannot be
rate-limited during a demo**. OpenAI's text-embedding-3-small is supported by the same module
and switches with one environment variable, but it is 1536-dimensional, so the catalogue must
be re-ingested — a vector written by one model cannot be searched with another.

**Q4. How do you know a change to retrieval did not break something else?**
`scripts/eval_retrieval.py` holds 46 real phrasings with the constraint each must satisfy, and
**a case passes only if every returned product satisfies it** — "one of three was right"
hides the miss the customer actually sees. It also replays every ✓/✕ the owner has given and
asserts approved products are still in the top three and rejected ones are not. The run exits
non-zero if a single case fails. It is currently 46/46, with no LLM involved at all.

**Q5. Your accuracy claim is on 46 cases you wrote yourselves. Isn't that marking your own
homework?**
Yes, partly — it is a regression suite, not a user study, and we would not claim otherwise.
Its value is that it is adversarial by construction: the first six cases are the failures we
actually shipped and had to fix, including one that returned nothing at all. For a real
measure we would need click-through or purchase data from live customers, which a one-week
project with a test number cannot produce. That is stated in the limitations.

---

## 4. The assistant — tools, memory and trust boundaries

### How it works

Free text goes to a language model served by Groq over an OpenAI-compatible API — primary
`openai/gpt-oss-20b`, then `gpt-oss-120b` and `qwen3.8-27b`. It is given **eight tools**:
`search_products`, `get_product_details`, `get_my_orders`, `get_my_cart`, `add_to_cart`,
`remove_from_cart`, `hide_product`, and `send_reply`, which must be the last call of every
turn.

**Budgets.** At most 5 model rounds per customer message, a 25-second wall-clock budget, at
most 4 products per reply, and the last 8 messages of history truncated to 350 characters
each — the Groq free tier gives 8,000 tokens per minute *per model*, and a call costs roughly
1,000–1,500.

**The trust boundaries, all enforced in code, not in the prompt:**

- **Identity is ours.** No tool takes a user id. The server passes the sender's id
  positionally and strips any `user_id` the model tried to supply. The model cannot read or
  modify another customer's cart, orders or history.
- **Products must exist.** `send_reply` accepts only ids; each is resolved against the
  catalogue and dropped if it is unknown or out of stock. If the model names ids that do not
  resolve, it gets an error back telling it to use ids from `search_products`.
- **Adding to the cart needs consent and a real size.** The customer's *current* message must
  read like a request to add; the size must be one the customer actually typed (or the only
  size the product has); and it must match stock. A bare "yes" only counts when the bot's own
  previous message offered to add that item.
- **Money is verified.** Any ₹ amount in the model's own sentence must appear in the
  catalogue's prices or MRPs, in the customer's words, or in a tool result. First violation:
  the model is told to send the reply again. Second: the offending sentences are deleted. This
  exists because a live chat once said *"your current total is ₹17,498"* for a ₹10,798 cart.
- **Cart claims are checked.** If the reply claims — or promises — an add or removal that no
  tool performed, the model is corrected once, then the claim is cut and replaced with
  instructions to reply `ADD` with a size.
- **It is a shop assistant, not a chatbot.** Replies that look like source code are replaced
  with a polite off-topic line, after a live tester asked it to write Python. It over-corrected
  once — refusing to show a customer their own address — so questions about the customer's own
  orders, cart, address, sizes and payments are explicitly never off-topic.

**Facts answered by code before the model runs at all:** the account-link code, every exact
command, greetings, "what is my address", re-order requests, and short cart/total/order
lookups. The model also receives the real cart in its context, labelled as the truth it must
not recompute.

**Degradation.** A 401 stops immediately. A rate limit moves to the next model; if every model
is exhausted and one will be free within 8 seconds, it waits once and retries. If everything
fails — or the turn exceeds 25 seconds, or five rounds pass with no reply — the webhook falls
back to `bot/chat.py`, which answers with plain retrieval and no model. Groq occasionally
rejects its own tool call with HTTP 400; the failed generation is parsed out of the error body
and repaired, including stripping chat-template tokens that leaked into the tool name.

### Why it is built this way

The alternative is to let the model write the whole reply. That is how most demos are built,
and it is exactly what fails in front of a panel: a hallucinated price, a product that does
not exist, an arithmetic error on a cart total. By making the model a *router* — it decides
what to do, code decides what is shown — the failure modes shrink to "it asked an unnecessary
question", which is survivable.

### Five questions the panel may ask

**Q1. Which model do you use and why that one?**
Groq's hosted `gpt-oss-20b`, with two fallbacks. Groq is OpenAI-compatible, so the client code
is standard, the free tier is fast enough for chat, and each model has its own token budget, so
falling back roughly triples throughput. We are not tied to it: the base URL and model names are
environment variables.

**Q2. How does the model know what is in my cart?**
We put it there. The context block we build for each turn includes the customer's name,
remembered preferences, recent purchases, the current cart with its real total, and the last
numbered list shown. The model can also call `get_my_cart`, but either way the numbers come
from PostgreSQL, not from its memory.

**Q3. What stops it from hallucinating a product or a discount?**
Three layers. It can only pass ids, which must resolve to real stocked products. Lists, prices
and photos are rendered by code. And every rupee amount in its own prose is checked against the
catalogue, the customer's words and the tool results — retry once, then the sentence is
deleted. We can demonstrate all three with the assistant test suite, which drives a scripted
fake model, so those checks run deterministically with no API quota.

**Q4. What happens when the model is rate-limited mid-conversation?**
It tries the next model, and if all are limited and one frees up within eight seconds, it waits
once. Beyond that it falls back to plain retrieval — the customer still gets products, just
without the conversational phrasing. The 429 path is one of the 74 checks in
`scripts/test_assistant.py`.

**Q5. If the model is only routing, why use one at all? Couldn't rules do this?**
Rules do handle a surprising amount — every exact command, greetings, lookups and address
questions never reach the model. The model earns its place on the open-ended half: knowing that
"something for my sister's mehendi, she likes pastels" needs one clarifying question rather than
a search, or that "will 1 suit a winter wedding?" refers to the first item shown. Writing those
rules by hand is where the previous keyword version failed.

---

## 5. Identity — linking a browser session to a phone number

### How it works

Meta's webhook gives us two things: a phone number and a message body. No cookie, no session,
no authorisation header. So a customer signed in on the website is, by default, a complete
stranger to the bot.

The bridge is a **single-use code**:

1. On `/account`, a signed-in customer is shown a code like `LINK-a7f3c9` and a `wa.me` deep
   link that pre-fills it. The token is 8 URL-safe characters, valid **10 minutes**, and
   minting a new one retires every earlier unused code for that account.
2. The customer taps the link and sends the message from their own phone.
3. The webhook checks for the `LINK-` prefix *before* anything else, validates the token —
   unknown, already used or expired are all refused — and binds that phone number to the
   account.
4. From then on, every message from that number is authenticated.

**The merge is the hard part.** By the time most customers link, they have already chatted with
the bot, so a bot-created user row already owns their phone number. In one transaction we
re-point that row's orders, sessions, link tokens, logged replies and feedback to the website
account; merge the carts line by line (adding quantities where the same product and size exist
on both sides, because re-pointing a duplicate would violate the cart's unique constraint);
close the orphan's active session if the target already has one, because a partial unique index
allows only one active session per user; then **delete** the orphan row and flush before
assigning the number.

Deleting rather than blanking is not a style choice. A row with neither email nor phone number
violates the named check constraint `ck_users_has_identity`, and the flush must land before the
number is reassigned or the unique index still sees the old row holding it. The first
implementation blanked the number first and failed live with exactly that constraint error.

### Why it is built this way

The obvious alternative — trust the phone number alone — is an account takeover waiting to
happen: anyone who knows your number could claim your order history. The code makes the trust
explicit and one-directional: we trust the number only *after* a secret generated for an
authenticated browser session arrives from it. That is the same shape as an email confirmation
link, which is the sentence to use if challenged.

### Five questions the panel may ask

**Q1. Why can't the website just tell the bot who is logged in?**
Because there is no channel between them. Meta delivers only the sender's number and the text;
there is no way to attach a session cookie to a WhatsApp message. The link has to be carried by
the customer, which is what the deep link does.

**Q2. Couldn't someone spoof a phone number and steal an account?**
Spoofing the number alone gets you nothing: the binding only happens when a valid, unused,
unexpired, single-use code arrives, and that code is only ever shown inside a signed-in browser
session. The attacker would need the victim's logged-in session first, at which point the phone
number is the least of their problems.

**Q3. What happens if the customer already ordered through the bot before linking?**
Their history comes with them. Orders, conversations, cart lines, logged replies and personal
"not for me" choices are re-pointed to the website account in one transaction, and the
duplicate user row is deleted. `scripts/test_linking.py` tests exactly this ordering, because
it is the realistic one.

**Q4. Why is the whole merge in a single transaction?**
Because a half-applied merge would leave orders pointing at a user row that no longer exists.
Either every row moves and the duplicate disappears, or nothing changes and the customer can
retry with a fresh code.

**Q5. Your codes are eight characters. Is that not brute-forceable?**
They are single-use, expire in ten minutes, are invalidated when a new one is minted, and must
arrive from a WhatsApp number to a business number that, on a test account, only accepts
allow-listed senders. The attack would have to guess a live code inside that window and send it
from a registered handset. For production we would add rate limiting per sender and lengthen
the token — it is a tunable, not a redesign.

---
## 6. Cart, checkout and payments

### How it works

**One cart, two channels.** `cart_items` is keyed by `user_id`, so the same rows serve chat
and web. Writes are a single `INSERT ... ON CONFLICT DO UPDATE` on the unique constraint
`(user_id, product_id, size)`, so a double-tapped button or two messages arriving together
increment a quantity instead of racing into an error. Size is `NOT NULL` with `""` meaning
"not chosen yet", because a NULL would silently defeat that unique constraint — PostgreSQL
treats NULLs as distinct.

**Checkout** requires a size on every line and a delivery address (validated: 10-digit
mobile, 6-digit PIN). The address is **snapshotted onto the order**, and so are the product
name and price of every line, so an order records what was actually bought at the price
actually paid, even if the catalogue changes later. If an unpaid order already exists with
exactly the same lines, it is reused rather than duplicated; change a quantity or a size and
you get a new one.

**Payment.** We create a Razorpay order and send a *Pay Now* button pointing at our own
`/pay/{order_id}` page, which opens Razorpay Standard Checkout. That page needs no sign-in —
nothing on it is secret, the key id is public, and the amount comes from our database, never
from the URL.

**Confirmation arrives by three independent paths, any one sufficient:**

1. **Browser verify** — the checkout handler posts the payment id, order id and signature to
   `/payments/verify`. We recompute the HMAC-SHA256 of `"{order_id}|{payment_id}"` with our
   key secret and compare in constant time. A forged post fails.
2. **Signed webhook** — Razorpay POSTs `payment.captured`; we verify HMAC-SHA256 over the
   **raw request bytes** with the webhook secret. Parsing the JSON and re-serialising would
   change whitespace and key order, and the signature would never match.
3. **Reconciler** — a background task started in the app lifespan asks Razorpay every
   **5 seconds** about unpaid orders from the last **60 minutes**, in batches of 10. The
   webhook is the fast path; the reconciler is the guarantee.

**Exactly once.** All three call `mark_order_paid`, which takes `SELECT ... FOR UPDATE` on
the order row, returns `False` if the status is already `captured`, and otherwise flips it,
records the payment id, and decrements **only the ordered quantities** from the cart. Only the
first caller gets `True`, and only that caller sends the WhatsApp confirmation. Without the
lock, two paths arriving together would both read "created" and the customer would get two
confirmations — that is tested by firing both simultaneously.

**Failure behaviour.** If Razorpay is unreachable at checkout, the order just created is
cancelled so a retry starts clean, and the chat says plainly that nothing was charged and the
cart is unchanged. If Razorpay is unreachable at the callback, the page shows *pending* rather
than an error, because the webhook or the reconciler will still record it.

**Two decisions that look odd until explained.** Chat orders return the customer's browser to
`https://wa.me/<number>` — the chat — not to our site, because a phone browser that had never
visited our ngrok domain hit ngrok's free-plan "You are about to visit" interstitial at the
exact moment of paying. That removed one confirmation path, which is *why* the reconciler
exists. And the column still called `payment_link_id` now holds a Razorpay *order* id: on
23 September a live CHECKOUT failed with `Razorpay 429: test mode limit of 30 reached for
payment_link` — test mode allows 30 payment links per account **ever**, cancelling does not
free them, and our own test runs had spent them. We migrated to Orders plus Standard Checkout
that day; orders created before the migration still confirm through the legacy path.

### Why it is built this way

Money is the one place where "usually works" is not acceptable, and the two failure modes are
opposite: miss a payment and the customer is angry; double-count it and the shop is
dishonest. Multiple redundant paths solve the first; the row lock and the idempotent status
check solve the second. Everything else — snapshots, address on the order, cancelling on
failure — exists so that the record of what happened cannot drift from what actually happened.

### Five questions the panel may ask

**Q1. Walk me through a payment from tap to confirmation.**
CHECKOUT creates an order in PostgreSQL with the address and line snapshots, then a Razorpay
order. The customer taps Pay Now, which opens our `/pay/{order_id}` page running Razorpay
Standard Checkout. On success the browser posts the signature to us; we verify it, mark the
order captured under a row lock, remove those quantities from the cart, and send a WhatsApp
confirmation. Independently, Razorpay's signed webhook and our reconciler do the same thing,
and whichever arrives second is a no-op.

**Q2. How do you verify the payment is genuine and not someone typing a URL?**
Two HMAC-SHA256 signatures with different keys and different payloads: the checkout signature
over `order_id|payment_id` with the API key secret, and the webhook signature over the raw
request body with the webhook secret. Both use constant-time comparison so a forged signature
cannot be tuned by timing. We never trust query parameters — the older callback path asks
Razorpay directly instead of believing `status=paid` in the URL, and the test suite posts a
forged `status=paid` callback to prove it is rejected.

**Q3. What if the customer pays but the network drops before your server hears about it?**
Three chances. The browser post may be lost, the webhook may be delayed or misconfigured, but
the reconciler polls Razorpay every five seconds for an hour after the order was created. The
confirmation is late at worst, not missing.

**Q4. Why no Razorpay SDK?**
Because it is synchronous. In an async FastAPI app it would block the event loop or need a
thread per call. `payments.py` is a thin `httpx` wrapper over three endpoints with no business
logic in it, which is also easier to fake in tests — every suite except one live check runs
completely offline.

**Q5. You are in test mode. What is actually missing before real money moves?**
Razorpay KYC and live keys, a verified WhatsApp business number, and HTTPS on a real domain
rather than a tunnel. The logic that changes is none — the same endpoints, signatures and
idempotency apply. What we would add operationally is refunds, a reconciliation report for the
owner, and alerting when the reconciler finds orders it cannot confirm.

---

## 7. Data model

### How it works

Seven tables, all with timezone-aware timestamps:

| Table | Holds | The constraint worth knowing |
|---|---|---|
| `users` | one identity per person, either channel | `ck_users_has_identity`: email or WhatsApp number must be present |
| `sessions` | conversation state: last products shown, selected product, messages, pending address | partial unique index — **one active session per user** |
| `cart_items` | the shared cart | unique `(user_id, product_id, size)`; quantity > 0 |
| `orders` | header: channel, status, total, Razorpay ids, address snapshot | total is `NUMERIC(10,2)`, never float |
| `order_items` | lines with **copied** product name and price | quantity > 0 |
| `link_tokens` | single-use linking codes | `used_at` stamped on first use |
| `bot_replies`, `feedback` | every answer, and every rating on it | feedback cascades when a user is deleted |

**Products are deliberately not in PostgreSQL.** The catalogue is a JSON file, embedded into
Chroma and read through `catalog.py`. Orders copy the product name and price at purchase time,
so the order record survives any catalogue edit.

**Three different "schemas" exist and only two are validated**: PostgreSQL tables with real
types and constraints; Pydantic models (`ProductMatch`) validated in memory; and Chroma
metadata, which is untyped dicts. `ProductMatch.from_raw()` accepts both key styles and is the
boundary where a renamed key becomes a visible error instead of a product priced at ₹0.

Schema changes are handled by `create_all()` plus a short list of idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements, because `create_all` creates missing
tables but never missing columns and there is no Alembic.

### Why it is built this way

One `users` table rather than separate web and chat tables is the decision the whole project
rests on: two tables would recreate the duplicate-identity problem by design, and the shared
cart falls out for free once both channels key on the same id. Money as `NUMERIC(10,2)` rather
than float is the other non-negotiable — float money drifts, and the drift shows up as a
₹0.01 mismatch nobody can explain at exactly the wrong moment.

### Five questions the panel may ask

**Q1. Why are products not in the database?**
They are catalogue data, not transactional data: never updated by users, always read whole,
and needed in a vector store anyway. Keeping them in JSON means one source of truth for both
the site and the embeddings. The transactional record does not depend on it, because orders
snapshot the name and price they charged.

**Q2. Why NUMERIC and not FLOAT for money?**
Because binary floating point cannot represent 0.10 exactly, so sums drift. With NUMERIC(10,2)
every amount is exact to the paisa, and the same value goes to Razorpay after conversion to
integer paise.

**Q3. What does "one active session per user" mean and why enforce it in the database?**
The webhook assumes each customer has exactly one live conversation holding the last products
shown and the selected item. Enforcing it with a partial unique index means the invariant
cannot be violated by a bug in application code, only by a failed write we can see. It is also
why the account merge has to close an orphan's session before re-pointing it.

**Q4. How would you add a column today, without Alembic?**
One idempotent `ADD COLUMN IF NOT EXISTS` in the startup list, which is how the five columns
added during the sprint were handled. It is honest but not sufficient for production: it
cannot rename, backfill or roll back. Alembic is first on the list once this holds real data.

**Q5. Your `feedback` table stores embeddings as JSONB. Why not pgvector?**
Volume. A few hundred ratings are compared in Python in microseconds, and adding pgvector
would mean a different Postgres image for one feature. At tens of thousands of ratings the
similarity scan becomes the bottleneck and pgvector — or reusing Chroma — is the right move.

---

## 8. The storefront and live sync

### How it works

The website is server-rendered Jinja with **compiled** Tailwind CSS — not the CDN script, so
the site is fully styled with no internet at the venue. It has browse with filters and sort,
product pages with "you may also like", semantic site search using the same engine as the
bot, accounts, cart, checkout with address validation, and an order tracking page.

**Live sync** keeps an open browser tab honest when the customer is acting in chat.
`static/live.js` polls `GET /api/live` every **2 seconds** on signed-in pages. The response is
a fingerprint: every cart line as a sorted `(id, product, size, quantity)` tuple plus the
status of the ten most recent orders, hashed with SHA-1 and truncated to 16 hex characters,
along with the cart count. When the fingerprint changes, the page re-fetches itself and swaps
only its `<main>` element, and shows a small toast such as "Order #7 paid".

Four safeguards make that safe: it never swaps while the customer has focus in an input inside
`main`; it skips polling on hidden tabs and resumes on focus; it stops permanently on a 401;
and `/api/live` reads the user **without updating `last_active_at`**, so polling never turns
into a write. Only pages that opt in re-render — cart, account, order and payment result.

### Why it is built this way

WebSockets were the obvious choice and the wrong one for this deployment. The demo runs behind
an ngrok tunnel, on venue Wi-Fi, with a `--reload` server that restarts whenever a file
changes. Every one of those breaks a socket and needs reconnect-and-resync logic. A 2-second
poll of a 16-character fingerprint costs two small queries per idle tab and has no reconnect
path to get wrong. It is the boring choice, chosen on purpose.

### Five questions the panel may ask

**Q1. How does the website know the cart changed on WhatsApp?**
It asks, twice a second is too often and once a minute is too slow, so every two seconds. The
endpoint returns a hash of the cart lines and recent order statuses; if that hash differs from
the last one, the page re-renders its main content in place.

**Q2. Why polling and not WebSockets or server-sent events?**
Because this runs through an ngrok tunnel on conference Wi-Fi with a reloading dev server.
Polling survives every disconnection with no reconnect logic; a socket would need it, and that
logic would be the thing that fails on stage. At real scale with thousands of tabs, the
trade-off flips and we would move to SSE.

**Q3. Isn't polling every two seconds expensive?**
Each poll is two indexed queries and a hash, and it does not write anything — that is why the
user lookup skips touching `last_active_at`. Hidden tabs stop polling entirely. For one shop
with a handful of open tabs it is nothing; for many, the fingerprint would move to Redis.

**Q4. What happens if the customer is typing when an update arrives?**
The swap is skipped and the fingerprint is *not* recorded as seen, so the next poll retries.
Without that check, a re-render would wipe a half-typed quantity or address. It is one of the
15 browser-logic checks in `test_live_js.mjs`, which runs the real `live.js` in Node against a
fake DOM.

**Q5. Why server-rendered pages rather than React?**
Time and reliability, with an exit path. Every page has a JSON twin under `/api/*`, so a
React or Next.js front end can be built against the same endpoints without touching the
backend. Choosing SSR meant the storefront worked on day one and has no build step to fail at
the venue.

---

## 9. Owner training — improving the assistant without retraining it

### How it works

Every free-text answer is logged: the question, the reply, the product ids in the order they
were shown, and which path answered it. On `/admin/training` — restricted to the emails in
`ADMIN_EMAILS`, everyone else gets 403 — the store owner sees each conversation and can:

- **Rate a product ✓ or ✕ for that question.** The question's embedding is stored with the
  rating. Later, when any question arrives whose embedding is within **cosine ≥ 0.75** of it,
  the net rating for that product (clamped to ±1) re-orders the results — in the bot *and* in
  the website search. "Saree for office" and "office saree for work" are the same question;
  "sherwani for my wedding" is not.
- **Rate a whole reply 👍 or 👎, with a note.** An approved reply becomes a few-shot example
  in the system prompt for similar questions; a note on a rejected reply becomes a rule. At
  most two of each are included, to spare tokens.
- **Undo anything**, and re-rating replaces rather than accumulates.

Customers have a narrower power: the **Not for me** button under any photo, or saying "not the
second one". That hides the product **for that customer only** — it is removed from their
results entirely, not just re-ordered. One customer's taste can never move the shop's ranking.

Rules are cached for 30 seconds, so a rating takes effect almost immediately without a query
per search.

### Why it is built this way

The honest framing, and the one to use with the panel: **this is not fine-tuning.** A few
dozen ratings cannot fine-tune a model, and the free tier does not allow it. What changes is
*what the model is shown* and *how results are ordered* — retrieval-time personalisation plus
prompt examples. That is also why it is instant and reversible, which fine-tuning is not.

### Five questions the panel may ask

**Q1. What does "training" mean here, exactly?**
Three mechanisms: a per-product score that re-ranks results for similar questions, approved
replies used as few-shot examples, and owner notes injected as rules in the prompt. No model
weights change anywhere in the system.

**Q2. How do you decide two questions are "similar"?**
Cosine similarity between the stored question embedding and the new query, at a threshold of
0.75 for ranking, and a looser 0.65 for prompt examples where a near-miss is still useful.
Both embeddings come from the same model used for the catalogue, so they live in one space.

**Q3. Could the owner's ratings make search worse?**
They can, which is why the effect is bounded and reversible: the net rating is clamped to ±1
so no product can be pinned to the top forever, ratings only apply to similar questions, the
sort is stable so unrated products keep their semantic order, and every rating has an Undo.
The evaluation script also replays every rating as a regression test, so a rating that
contradicts the catalogue shows up as a failing run.

**Q4. Why can a customer hide a product but not down-rank it for everyone?**
Because one customer's dislike is not evidence about the shop. Their hide is applied by
removing the product from their own results before ranking is even consulted; the owner's
rating is global but only re-orders. Different powers, deliberately.

**Q5. Isn't this just a workaround for not doing real machine learning?**
It is a deliberate choice about where learning belongs. With a few dozen ratings, fine-tuning
would overfit and could not be undone; re-ranking at retrieval time is instant, explainable
and reversible, which is what a shop owner actually needs. The same mechanism scales: with
thousands of ratings it becomes training data for a learned re-ranker, which is the natural
next step rather than a different design.

---
## 10. The technology stack, and why each piece

### How it works

| Layer | Choice | Why this, and what was rejected |
|---|---|---|
| Language | Python 3 | One language for the web app, the embeddings and the scripts |
| Web framework | FastAPI + Uvicorn | Async suits a webhook that fans out to Chroma, PostgreSQL and the Graph API, and Meta retries anything slow. Flask would have meant threads per request or blocking the webhook |
| HTTP client | `httpx` (async) | Non-blocking calls to WhatsApp, Razorpay and the LLM. `requests` is synchronous |
| Database | PostgreSQL 15 + SQLModel | Real constraints, transactions, row locks and `NUMERIC` money. SQLite has no `SELECT ... FOR UPDATE` worth the name and one writer at a time |
| Vector store | ChromaDB in Docker | Local, free, no account, persistent volume. Pinecone or Weaviate Cloud would add a signup, a key and an outage we cannot fix during a demo |
| Embeddings | all-MiniLM-L6-v2, 384-dim, on CPU | Free, offline, **cannot be rate-limited during a demo**. OpenAI's 1536-dim model is a one-variable switch, but it needs a key and re-ingestion |
| Language model | Groq `gpt-oss-20b` + two fallbacks | OpenAI-compatible API, fast, free tier; per-model token budgets mean fallbacks triple throughput. GPT-4o-mini works through the same client if a key is present |
| Messaging | WhatsApp Cloud API v21.0 | The official API: text, images, interactive buttons and media upload. Unofficial libraries risk the number being banned |
| Payments | Razorpay (test mode), no SDK | UPI, cards and netbanking in India; hand-written `httpx` wrapper because the SDK is synchronous |
| Front end | Jinja2 + compiled Tailwind + vanilla JS | Works offline at the venue, no build step to fail; every page has a JSON twin so a React front end can replace it later |
| Security | bcrypt, signed session cookies, HMAC-SHA256 | Password hashing with a 72-byte guard, signed (not encrypted) cookies holding only a user id, constant-time signature checks |
| Infrastructure | Docker Compose, ngrok | Identical PostgreSQL and Chroma on both machines; a public HTTPS URL for Meta's webhook |

Thirteen Python dependencies in total. Notably absent: no `razorpay` SDK, and no
`sentence-transformers` — the local embedding model comes through Chroma's default embedding
function, which downloads about 80 MB once and caches it.

### Five questions the panel may ask

**Q1. Why FastAPI over Django or Flask?**
The workload is a webhook that must acknowledge in milliseconds while doing several seconds of
I/O across three external services. FastAPI is async-first, so those calls overlap instead of
blocking, and it gives typed request models and automatic docs for free. Django's strengths —
admin, ORM conventions, templates — are aimed at a different shape of application.

**Q2. Why ChromaDB and not FAISS, pgvector or a hosted vector database?**
FAISS is a library, not a service: we would have to manage persistence, metadata and the
server ourselves. pgvector would have been a reasonable choice and keeps one datastore, but it
means a custom Postgres image; Chroma runs as a container with a volume and speaks metadata
filters out of the box. Hosted options add an account, a key, cost and an outage we cannot
debug on stage.

**Q3. Why not fine-tune a model on your catalogue?**
Because the catalogue changes and fine-tuning does not: a new product would require retraining,
while retrieval sees it the moment it is ingested. Fine-tuning also cannot fix the failure that
matters — a model stating a price — whereas grounding every fact in code does.

**Q4. What does this cost to run?**
Today, nothing: local embeddings, Groq's free tier, Razorpay test mode, Docker on a laptop.
Running it for a real shop, the costs are WhatsApp conversation charges billed by Meta, a small
server, and the LLM — roughly 1,000 to 1,500 tokens per message, which on a paid tier is a
fraction of a rupee per conversation.

**Q5. If you started again tomorrow, what would you change?**
Three things. Put Alembic in from the first commit. Design for multiple stores from the start —
a `store_id` on every table costs nothing on day one and is painful to add later. And write the
retrieval evaluation harness *before* the retrieval code, because it is what turned "the bot
feels better now" into a number we could defend.

---

## 11. Testing and evidence

### How it works

Nine suites, **434 checks**, all runnable on a laptop with Docker up and no paid API keys.
The counts below are what each suite printed when it was run on the morning of 23 September;
every one finished with zero failures:

| Suite | Checks | What it proves |
|---|---|---|
| `scripts/test_cart.py` | 126 | Cart operations, **eight concurrent adds** against the atomic upsert, checkout, purchase-history contract, cart surviving account linking, and the same cart across both channels |
| `scripts/test_assistant.py` | 74 | The assistant driven by a **scripted fake model** — no network, no quota: tool binding, memory, code-rendered lists, refusal to invent products, consent before adding, fallbacks, and "it is a shop assistant, not a chatbot" |
| `scripts/test_payments.py` | 61 | Amounts and signatures, one order per checkout, raw-body HMAC, a **forged `status=paid` callback rejected**, simultaneous confirmations under the row lock, Razorpay down, and one real ₹1 call to the live test API that is skipped rather than failed if keys are absent |
| `scripts/test_store.py` | 36 | Filters, listings, site search, similar products, safe sign-in redirects, address validation, and the product-code round trip from website to chat |
| `scripts/test_training.py` | 28 | Owner ratings re-rank similar questions, Undo restores order, customer hides stay personal, non-admins get 403, and **no test rating is left behind** |
| `scripts/test_linking.py` | 18 | The realistic ordering: chat first, then signup, then the merge; replayed and garbage codes refused |
| `scripts/test_live.py` | 20 | `/api/live`, the fingerprint, which pages opt in, and a live HMAC-signed capture flipping an order |
| `scripts/test_live_js.mjs` | 15 | The real `live.js` executed in Node against a fake DOM — including "do not clobber the customer mid-typing" and "stop polling after a 401" |
| `scripts/eval_retrieval.py` | 46 cases + 10 rules | Retrieval quality with no LLM: every returned product must satisfy the query, plus every owner rating replayed as a regression. Today: **46/46 and 10/10** |

Tests never touch paid services: LLM keys are blanked or faked, Razorpay and WhatsApp are
monkey-patched, and the reconciler is disabled so it cannot race the assertions. They do need
PostgreSQL, because they exercise the real constraints — which is the point.

### Five questions the panel may ask

**Q1. How do you test a chatbot whose replies are non-deterministic?**
By replacing the model with a script. `test_assistant.py` substitutes the model call with a
prepared sequence of tool calls and messages, so every assertion is deterministic and free. It
also records what the model was *sent*, so the prompt and context block are testable too. The
non-deterministic part — phrasing — is exactly the part we do not assert on, because code, not
the model, produces every fact.

**Q2. How do you test payments without spending money?**
Razorpay is faked for all 61 checks that ran today, including the signature maths, which is
pure computation verifiable against known inputs. One optional section makes a single ₹1 call
to the live test API to prove the credentials and endpoint still work; it reports SKIP rather
than FAIL when keys are absent, which is why the suite prints 61 rather than 62.

**Q3. Which test do you value most?**
`test_linking.py`. The account-merge bug only appears in the realistic order — customer chats
first, signs up later — and a test that linked a fresh account would have passed while the bug
waited for demo day. It caught a check-constraint violation on an *intermediate* state, which
is the kind of thing that never appears in a happy-path test.

**Q4. What is your test coverage?**
We measure by behaviour rather than line coverage: every user-visible path — search, selection,
cart, linking, checkout, payment, confirmation, training, live sync — has a suite, and the
counts are printed by each run. We do not claim a coverage percentage, and the thinnest areas
are the templates and the admin UI, which are exercised only through their routes.

**Q5. Your docs say 68 assistant checks but the file has 74. Which is right?**
The file. The count in `CLAUDE.md` was written when the suite was smaller and was not updated —
the same class of mistake the project report calls out, where a status document that lies is
worse than none. The verifiable artefact is the run itself: each suite prints its own passed
and failed counts, and we can run any of them on request.

---

## 12. Limitations and future scope

### Stated honestly, before the panel finds them

1. **Stock is displayed, never decremented.** `in_stock` is true for all 84 products. Real
   inventory needs reservation with expiry and locking, not a boolean — deliberately deferred.
2. **One store.** Credentials and the catalogue are configured for a single shop; serving many
   needs a `store_id` on every table and per-store credentials.
3. **No schema migrations.** Additive `ADD COLUMN IF NOT EXISTS` only; Alembic is required
   before this holds real data.
4. **Meta test number.** Recipients must be allow-listed, and free-form replies only work
   within 24 hours of the customer's last message. Outside that window, production requires
   approved message templates.
5. **Tunnel deployment.** ngrok, with a dev server; session cookies are not HTTPS-only because
   local development is plain HTTP.
6. **A dev-only route exists.** `POST /dev/send-test` has no authentication and must be
   removed before any real deployment — it is marked as such in the code.
7. **Retrieval rules are domain-specific.** Garment vocabulary and occasion words are written
   for Indian ethnic wear; another catalogue needs its own mapping, ideally derived from the
   catalogue rather than hand-written.
8. **Language.** English and light Hinglish only; no voice notes, no image search.
9. **Evaluation is a regression suite, not a user study.** 46 cases we wrote, not click-through
   from real customers.

### Where it goes next

- **Phase 2 engineering:** Alembic migrations, real stock reservation, refunds, fulfilment and
  courier tracking, and production credentials on a verified number.
- **Multi-store product:** the natural commercial path — `store_id` everywhere, Shopify and
  WooCommerce catalogue sync, per-store WhatsApp numbers through Meta's embedded signup.
- **Revenue features for shops:** abandoned-cart reminders, and cash-on-delivery order
  confirmation, which measurably reduces refused deliveries in Indian e-commerce.
- **Assistant:** voice notes transcribed to text, image search ("find me something like this
  screenshot"), and regional languages.
- **Learning:** with thousands of owner ratings, replace the hand-tuned re-rank with a learned
  re-ranker trained on them.

### Five questions the panel may ask

**Q1. What does not work in this project?**
Stock is not real, there are no migrations, it runs on a test number behind a tunnel, and it
serves one store. Those are all listed with reasons, and none of them is a logic defect — they
are the difference between a working prototype and an operated service.

**Q2. What would it take to put this in a real shop next month?**
Razorpay KYC and live keys, a verified WhatsApp business number with approved templates, a real
host with HTTPS, Alembic for migrations, and stock reservation. About two weeks of engineering,
most of it operational rather than algorithmic.

**Q3. Can this work for a shop that is not ethnic wear?**
The architecture yes, the vocabulary no, not without work. The garment-to-category mapping and
occasion words are hand-written for this domain. The generalisation is to derive that mapping
from each store's own catalogue at ingestion time, which is the first thing we would build for
a second store.

**Q4. What is the commercial case?**
Small sellers already sell over WhatsApp by hand, and the features that make money are
adjacent to what we built: abandoned-cart recovery and COD confirmation, which cuts refused
deliveries. A shop with 1,000 COD orders a month and a 25% return-to-origin rate loses around
₹50,000 a month; a confirmation step that brings that to 15% pays for the service many times
over.

**Q5. What did you learn that you did not expect?**
That the interesting problems were not the AI ones. Embeddings rank but do not constrain;
constraints fire on intermediate states, not just final ones; a webhook must acknowledge even
when its own work failed; and an integration's free tier can have a permanent quota — test mode
allows thirty payment links per account ever, and our own test runs spent them, which forced a
redesign on the last day.

---

## Appendix A — What broke, and what it taught

These are the strongest answers available, because they are specific, dated and fixed. Each
came from a real failure during the sprint.

| What happened | Cause | Fix and lesson |
|---|---|---|
| **The webhook retry storm.** Messages logged, no reply, the same message three times | The access token had expired; the send raised an error type the handler did not catch, so FastAPI returned 500 and Meta re-delivered into the same failure | Swallow send failures, catch broadly, always acknowledge 200. *A webhook must acknowledge receipt even when its own work failed.* The 24-hour dashboard token was replaced with a System User token that never expires |
| **The merge that violated its own constraint.** Linking failed exactly when the customer had chatted first | The merge blanked the orphan row's phone number before deleting it; for that instant the row had neither email nor phone, violating `ck_users_has_identity` | Delete the row and flush, then assign the number. *Constraints fire on intermediate states.* Found by a test that reproduced the realistic ordering — a fresh-account test would have passed |
| **Chroma rejected our vectors** with "expected a list of floats, got np.float32" | `list(vector)` yields numpy scalars, not Python floats | `vector.tolist()`. *"Convert to a list" and "convert to a list of native types" are different operations* |
| **Retrieval ignored the important word:** "groom outfit budget 12000" returned a women's kurta set | The hypothesis was noisy embedded text. Tested directly by embedding both versions: 0.652 versus 0.590 on the same wrong products — **cleaning changed nothing** | Gender became a filter applied after ranking. *Test the hypothesis before acting on it. Embeddings rank, they do not constrain* |
| **The bot did arithmetic from memory:** "your current total is ₹17,498" for a ₹10,798 cart | The model summed prices itself | Every ₹ amount it writes must exist in the catalogue, the customer's words or a tool result; retry once, then delete the sentence |
| **The bot claimed it had added an item** — "Sure, I'll add the Silver Paisley Mojari in UK 11" — and the cart was empty at checkout | A promise is not an action | A claimed cart change with no successful tool call is sent back once, then cut |
| **"hi" returned three random kurtas** | The greeting was being searched | Whole-message greeting matching, so "high neck kurta" and "hi i need a kurta" stay searches |
| **It wrote Python when asked**, then over-corrected and refused to show a customer their own address | Off-topic handling that was first absent, then too broad | Code-looking replies are replaced; the customer's own orders, cart, address, sizes and payments are explicitly never off-topic |
| **Razorpay's permanent quota.** A live CHECKOUT failed with "test mode limit of 30 reached for payment_link" | Test mode allows 30 payment links per account *ever*, cancelling does not free them, and our own test runs had spent them | Migrated to Razorpay Orders and Standard Checkout on our own `/pay` page, in one day, with the legacy path kept for older orders |
| **ngrok's interstitial at the moment of paying** | A phone browser that had never visited the tunnel domain saw "You are about to visit…" right after payment | Chat orders return to `wa.me` instead. That removed a confirmation path, which is why the background reconciler exists |
| **The test suite deleted the developer's real account** | An early cleanup truncated every table | Each suite now removes only the users it created, verified with a sentinel row that survives all runs |
| **Chroma's HNSW index skipped a product.** Asked for all 84 items it returned 83, missing EW080 | The index is approximate by design | Any catalogue id missing from the result is scored exactly from its stored vector, so nothing is unfindable |
| **A status document that lied.** `CLAUDE.md` listed webhook verification as done before it had been attempted | Status written from intention rather than observation | *A status file that lies is worse than no status file: it hides the real blocker* |

## Appendix B — Commands

```bash
# Start the infrastructure
docker compose up -d                 # PostgreSQL 5432, ChromaDB 8001
docker compose ps

# Run the app (from backend/)
uvicorn main:app --reload --port 8000
ngrok http 8000                      # then point Meta's callback at <url>/webhook

# Ingest the catalogue (after editing products.json or changing EMBEDDING_PROVIDER)
backend/.venv/Scripts/python scripts/ingest_catalog.py

# Evidence to show a panel
backend/.venv/Scripts/python scripts/eval_retrieval.py      # 46 cases + owner ratings
backend/.venv/Scripts/python scripts/test_assistant.py      # 74 checks, fake model
backend/.venv/Scripts/python scripts/test_cart.py           # 126 checks
backend/.venv/Scripts/python scripts/test_payments.py       # 61 checks (62 with live keys)
backend/.venv/Scripts/python scripts/test_linking.py        # 18 checks
backend/.venv/Scripts/python scripts/test_training.py       # 28 checks
backend/.venv/Scripts/python scripts/test_live.py           # 20 checks
backend/.venv/Scripts/python scripts/test_store.py          # 36 checks
node scripts/test_live_js.mjs                               # 15 checks

# Talk to the bot without a phone (server must be running)
backend/.venv/Scripts/python scripts/simulate_webhook.py --message "saree for office"

# Rebuild CSS after a template change (from backend/)
npx -y tailwindcss@3.4.17 -c tailwind/tailwind.config.js -i tailwind/input.css -o static/css/site.css --minify
```

## Appendix C — Corrections to the other documents

If a panel member reads the synopsis or the deck alongside the code, these four differences
will show up. Say the code is authoritative.

1. **Fields per product:** the file has **33**, not 32 as `CLAUDE.md` and the project report
   say.
2. **Assistant test count:** the suite has **74** checks; `CLAUDE.md` still says 68.
3. **Suite counts drift too** — the numbers in this document are the ones the suites printed
   on 23 September, not the ones quoted elsewhere. Re-run any suite to check.
4. **`docs/PROJECT_REPORT.md` is a Phase-1 snapshot dated 18 September.** Its war stories are
   accurate and valuable; its status is not — it describes 32 products and Razorpay as
   remaining work. `CLAUDE.md` is the current document.

---

*Prepared 23 September 2026 from the code as committed. Every claim above is either a constant
read from a source file or output printed by a test run.*
