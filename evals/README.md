# Conversation evals

The cases here are played through the real bot by `scripts/eval_conversations.py`
(plan.md, P2). Every turn is graded by code from facts: which exit answered, what
the model called, how the customer's cart, orders and session changed, and what the
catalogue says. A model judge is used only for what code cannot settle.

```
evals/
  README.md            this guide                                     (public)
  configs.yaml         models under test, brands -> stores            (public)
  cases/<brand>/*.yaml our cases                                      (public)
  catalogues/          fixture catalogues for brands not yet imported (public)
  baselines/           scores a change is compared with               (public)
  raw/                 exported real conversations                    (PRIVATE, gitignored)
  private/             real customers' words and media                (PRIVATE, gitignored)
  runs/                model outputs of each run                      (PRIVATE, gitignored)
```

Everything under `evals/` is private unless `.gitignore` adds it back by name, so a
new folder can never leak by accident. Never put a real customer's name, number or
address in a case. Rewrite what they said in your own words.

## Running

From the repo root, with Postgres up and the model's key in `backend/.env`:

```
backend/.venv/Scripts/python scripts/eval_conversations.py --list        # validate, list
backend/.venv/Scripts/python scripts/eval_conversations.py --dry         # every case plays; no quota
backend/.venv/Scripts/python scripts/eval_conversations.py               # kurta, first config
backend/.venv/Scripts/python scripts/eval_conversations.py --config gpt-oss-120b --only "cart-*"
backend/.venv/Scripts/python scripts/eval_conversations.py --save-baseline
backend/.venv/Scripts/python scripts/eval_conversations.py --resume evals/runs/<dir>
```

- **Free tier:** `tpm` in `configs.yaml` paces calls under the per-minute limit.
  Waits are left out of latency and out of the assistant's 25-second budget. When
  the daily limit is reached the run stops cleanly; `--resume` carries on later.
- **No fallbacks:** each config tests one model. If it fails, the turn is graded as
  that model's failure (`no_fallback`) and is never quietly answered by another.
- **Exit code** is 1 on a setup error, or on a case that passed in the baseline
  and fails now. Cases failing in the baseline are expected; that is what a
  baseline records.
- `scripts/test_eval_harness.py` tests the harness itself without any quota.

## A case

```yaml
- id: cart-pick-by-name-then-size          # unique within the brand
  source: replies 177-180 (22 Sep)          # where it came from (optional)
  tags: [cart, size]
  given:                                    # durable facts, written directly
    name: Asha                              # WhatsApp profile name
    profile: {gender: Men, notes: [likes pastels]}
    address: default                        # or a full address mapping
    linked: true                            # has a website account
    orders:
      - {items: [{product: EW045, size: XXL, qty: 1}], status: paid, days_ago: 5}
    cart: [{product: EW071, size: Free Size, qty: 3}]
  prelude:                                  # turns that set the scene, not graded
    - say: only male options for a night wedding
      model:                                # scripted answers: costs nothing
        - send_reply: {message: "Men's picks.", product_ids: [EW039, EW054, EW006, EW042]}
    - say: "1"                              # no `model`: answered with the model OFF
  turns:                                    # graded, by the model under test
    - say: choose blush pink one
      expect:
        selected: EW042
        cart_unchanged: true
    - say: "46"
      expect:
        cart_has: [{product: EW042, size: "46"}]
      judge: Confirms what was added in one short sentence.
```

The conversation state (memory, the list on screen, the item picked) is never
written by hand. Preludes produce it through the real routing, so it always has
the shape today's code writes. A prelude turn without `model` runs with the
assistant switched off, so commands, product codes and plain search answer it.
A turn with `model` is answered by those steps. A step is `{tool: args}` or
`{text: "..."}`; a list of them makes one response with several calls. If the bot
asks the scripted model for more steps than given, or leaves some unused, the case
is a setup error, not a grade.

A turn is `say: text`, `button: pick:EW006` / `hide:EW006`, or `media: image|audio`.
Quote numbers (`"46"`), and quote any text containing `: ` or `#`.

### Placeholders
`{order1}`, `{order2}` ... are the ids of the seeded orders. `{shown1}`,
`{shown2}` ... are the products on screen before the turn. They work in `say`
and `expect`.

### Expectations (`expect`)

| Key | Passes when |
|---|---|
| `path` | the exit that answered is one of these (`lookup*` matches a prefix) |
| `cart` | the cart is exactly these lines (`product`, optional `size`, `qty`) |
| `cart_has` / `cart_lacks` | these lines are in the cart / these products are not |
| `cart_unchanged` | the cart did (true) or did not (false) stay the same |
| `cart_units` | the cart holds this many units |
| `selected` | this product is the one picked |
| `shows` | products were shown (true) or not (false) |
| `shows_min` / `shows_max` | at least / at most this many shown |
| `shows_any` / `shows_none` | at least one of these shown / none of these shown |
| `shown_fit` | every product shown meets `gender`, `categories`, `max_price`, `min_price`, `occasion` (any of), `colour` (any of; matches colour and name) |
| `asks` | the reply asks a question (true) or not (false) |
| `reply_has` / `reply_lacks` | each regex (case-insensitive) matches / does not |
| `cta` | a Pay Now button was (or was not) sent |
| `order_placed` | an order was (or was not) created |
| `address_pending` | the bot is (or is not) collecting an address |
| `profile_gender` | the customer's saved gender is this (`null` = none) |
| `hidden_has` | these products are hidden for this customer ("Not for me") |

### Checked on every turn
`replied`, `claims_backed` (every "added"/"removed" matches a real cart change),
`pay_claim_backed` (no Pay Now talk without the button), `sizes_real` (sizes
offered exist for the products in focus), `amounts_real` (every rupee amount is a
price, a total or a number the customer gave), `no_fragments` ("Added the."),
`no_code`, `no_markdown`, `no_errors`, `no_fallback`, `cart_shown_once`.
If a turn legitimately breaks one, add `skip: [name]` and a `note` saying why.

### Judge
`judge:` is a one-line criterion graded by the `judge` model role (`--judge`;
set `LLM_JUDGE_MODEL`, `LLM_JUDGE_BASE_URL`, `LLM_JUDGE_API_KEY`). Use a model
family different from the one under test. Before judge scores decide anything,
check it agrees with the owner's own ratings (`raw/feedback.json`).

## Writing good cases
- Start from something that really happened, and name it in `source`.
- Expect the behaviour we want, not what the bot does today. A failing case is a
  to-do item; the baseline records it.
- Prefer facts (`cart_has`, `shown_fit`) over wording (`reply_has`). Use wording
  only where the words are the point (a total, an order number).
- One behaviour per case, so a failure says what broke.
