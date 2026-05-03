# Vera Bot — magicpin AI Challenge submission

A deterministic, context-grounded message engine for Vera. Implements the 5
HTTP endpoints (`/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`,
`/v1/reply`) and ships a `compose(category, merchant, trigger, customer?)`
function that picks one signal and writes a sharp, single-CTA message.

---

## Quickstart

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Or with Docker:

```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

Point the judge simulator's `BOT_URL` at `http://localhost:8080` and run.

To produce `submission.jsonl` for the 30 canonical test pairs:

```bash
python dataset/generate_dataset.py --seed-dir dataset --out expanded
python make_submission.py --expanded-dir expanded --out submission.jsonl
```

To run the local end-to-end smoke test:

```bash
python test_e2e.py
```

---

## Verified test results

The bundled `test_e2e.py` exercises every endpoint and every trigger kind
end-to-end. Latest run, against this submission:

- **5/5 endpoints** working (healthz, metadata, context, tick, reply).
- **Idempotence** verified: same `(scope, context_id, version)` returns
  `accepted: false, reason: stale_version`. Higher version replaces.
- **Seed dataset (25 triggers, 5 categories, 10 merchants, 15 customers)**:
  25 actions across 24 distinct trigger kinds, **0 quality issues** in the
  body sanity check (no empty bodies, no template-artifact leakage, no
  default-value leaks, no double-spaces, no trailing dashes).
- **Expanded dataset (100 triggers, 50 merchants, 200 customers)**: all
  **30/30 canonical test pairs** compose cleanly, average body length 223
  chars.
- **Reply handler (5/5 scenarios)**:
  - English commit ("Yes please go ahead") → `send` with action-mode body.
  - Hindi commit ("Theek hai bhej do") → `send` with action-mode body.
  - Auto-reply turn 1 ("Thank you for contacting us...") → `send` (one
    polite human-only nudge).
  - Auto-reply turn 2 (same canned text) → `end`.
  - Hostile ("Stop messaging me, this is useless spam") → `end`.
  - Busy ("Kal baat karte hain, abhi busy hoon") → `wait` (2h backoff).

---

## Approach

The brief makes three things explicit and load-bearing:

1. **Determinism.** Same inputs → same output. No randomness, no
   temperature.
2. **No fabrication.** Every number, name, date in the message must trace
   to a context field. Hallucinated facts get capped at 5/dimension.
3. **Decision quality.** Strong bots don't recite every fact — they pick
   the one signal that should drive the next message.

I optimized for these directly. The hot path is **rule-based**, with one
handler per `trigger.kind` (26 of them). No LLM call during composition.
This buys:

- **Zero-variance determinism** by construction.
- **Zero hallucination risk** — every value in the body is read out of the
  context dict; if a field is missing, the handler degrades to a framing
  that doesn't need it. The composer never invents a number, batch, or
  proper noun.
- **<10ms latency per `compose`** — well under the 30s ceiling, leaving
  budget for many actions per tick (the brief caps it at 20).
- **Auditability** — `rationale` strings explain exactly which signal
  drove the message and what fallback (if any) was taken.

The tradeoff is **less linguistic variety than an LLM would produce**. I
think that's the right trade for this rubric: variety doesn't score, but
specificity, groundedness, and trigger-relevance do — and rules-based wins
those by construction.

### How a handler works

Each handler reads the trigger's `payload` and the merchant's signals,
picks the **one anchor** that will make the message specific, and composes
around it. Examples:

| Trigger kind | Anchor the handler picks |
|---|---|
| `research_digest` | `merchant.customer_aggregate.high_risk_adult_count` if the digest segment is `high_risk_adults`; else lapsed-180d count; else generic relevance |
| `perf_dip` | First single most-fixable signal: `stale_posts:Nd` → name the day count; else CTR vs peer-median gap; else `no_active_offers`. Falls back to `merchant.performance.delta_7d` if the trigger payload is a placeholder. |
| `ipl_match_today` | `payload.is_weeknight=False` → counter-intuitive "skip the dine-in promo" call (per Case Study 5) |
| `recall_due` | Real slot labels from `payload.available_slots`; real cleaning offer from `merchant.offers`; honors `customer.identity.language_pref` for hi-en mix |
| `chronic_refill_due` | Senior-aware salutation if age band ≥65; pulls senior-discount + free-delivery from active offers; molecule names verbatim. **Falls back to a treatment-followup framing for non-pharmacy merchants** rather than fabricating meds. |
| `active_planning_intent` | **Skips qualifying** — delivers a drafted artifact based on the topic. Hard-coded artifact tiers for thali / wedding / kids-program / delivery; category-aware fallback for unknown topics. Directly addresses the intent-handoff failure the brief calls out. |
| `festival_upcoming` | When `payload.festival` is missing, surfaces category-typical festivals and asks the merchant to pick — instead of fabricating "the festival in days" |

26 trigger kinds total — 21 merchant-facing + 5 customer-facing. Plus a
generic fallback that never invents facts.

### Reply handling

`/v1/reply` runs four explicit detectors, in this order:

1. **Hostile / opt-out** (`stop`, `not interested`, `band karo`, ...) →
   `action: end` with a graceful exit rationale.
2. **Intent commit** — checked *before* auto-reply detection (a clear
   "yes"/"haan" is unambiguous and shouldn't be reclassified). Regex over
   English + Hindi commit phrases (`yes`, `let's do it`, `theek hai`,
   `kar do`, `bhej do`, ...). When matched, the bot switches to **action
   mode** — sends a per-kind action acknowledgment ("Drafting your
   SOP-update note now... share in 2 min") rather than asking another
   qualifying question. This is the Pattern D fix the brief calls out.
3. **Auto-reply detection** — substring match on canned phrases (`"thank
   you for contacting"`, `"team tak pahuncha"`, ...) **or** the same
   merchant message twice in a row (≥25 chars to avoid one-word matches).
   First hit: one polite "are you the human owner?" nudge. Second hit:
   `end`. This is Pattern B from the brief.
4. **Question / clarification** — answer crisply, never fabricate pricing,
   keep the next step alive.

Plus: wait-on-busy ("kal", "later", "in a meeting" → 2h backoff), 3-nudge
cap, and anti-repetition (if the next send would equal the last send
verbatim, swap to wait).

### Context handling

- `/v1/context` is idempotent on `(scope, context_id, version)`. Higher
  versions atomically replace; equal-or-lower versions return `accepted:
  false, reason: stale_version`.
- All scope shapes (`category`, `merchant`, `customer`, `trigger`) are
  accepted. Unknown scope → `accepted: false, reason: invalid_scope`.
- Storage is in-memory dict — fine per the brief.
- A bonus `/v1/teardown` endpoint wipes all state for clean test reruns.

### Why no LLM

I considered a hybrid (rules pick the anchor + LLM polishes prose) and
rejected it because:

- The judge specifically rewards **anchored numbers and source citations**,
  which are already locked in by the rules.
- Adding an LLM in the hot path means the bot can rephrase a real number
  into a wrong number under entropy. Per-call temperature=0 reduces but
  doesn't eliminate this — and verifying every output against the source
  dict is more code than the rules-based approach was.
- The 30s tick timeout + 20-action cap means the bot should plan for fast,
  many small composes. A rules path is single-digit milliseconds. An LLM
  path is hundreds of ms minimum and adds a cold-start failure mode.

If this submission gets to the replay round and the rubric there rewards
linguistic flair more than groundedness, the right next step is to wrap
the rules-composer output through a "polish only — do not change any
number, date, or proper noun" LLM pass. That keeps the groundedness
invariant while adding variety.

---

## What I'd want next

If I had more context, three things would meaningfully sharpen the
messages:

1. **Per-merchant peer cohort stats** — not just category-wide peer_stats
   but "your locality, your rating tier" — would let `perf_dip` say
   "you're underperforming Lajpat Nagar dentists by X%" rather than the
   city median.
2. **Timezone-aware trigger logic** — IPL match-time logic currently
   treats any non-weeknight as "shift to delivery", but a Friday 10pm
   match has a different curve than a Saturday 7pm one.
3. **Customer LTV** — to prioritize which lapsed customer to winback
   first. Right now I treat them by recency, not value.

---

## Files

```
vera-bot/
├── bot.py             # FastAPI app, 5 endpoints, in-memory stores
├── composer.py        # 26 per-kind handlers + the public `compose` entry
├── replies.py         # /v1/reply detectors and per-kind action snippets
├── make_submission.py # produces submission.jsonl from the 30 canonical pairs
├── test_e2e.py        # end-to-end smoke test
├── submission.jsonl   # the 30 canonical test-pair compositions
├── requirements.txt   # fastapi, uvicorn, pydantic
├── Dockerfile         # for deploy
└── README.md          # this file
```
