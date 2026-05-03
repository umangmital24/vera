"""
Vera bot — HTTP service exposing the 5 endpoints the magicpin judge calls.

Endpoints:
  GET  /v1/healthz   — liveness + how many contexts are loaded
  GET  /v1/metadata  — team identity
  POST /v1/context   — receive a context push (idempotent on (scope, id, version))
  POST /v1/tick      — periodic wake; bot decides what (if anything) to send
  POST /v1/reply     — receive a merchant/customer reply, return next move

State is in-memory. The brief explicitly allows this.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations
import os
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from composer import compose
import replies as rp

# -----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera")

START_TIME = time.time()

# In-memory stores. Keyed by (scope, context_id) for contexts.
contexts: Dict[tuple, Dict[str, Any]] = {}

# Track which suppression keys we've already sent for this conversation cycle
# so /tick doesn't re-send the same trigger twice.
sent_suppression_keys: set = set()
# Map suppression_key -> conversation_id we created for it (so reply lookups work).
suppression_to_conv: Dict[str, str] = {}


# ----- helpers ---------------------------------------------------------------

def _get_ctx(scope: str, cid: str) -> Optional[Dict[str, Any]]:
    rec = contexts.get((scope, cid))
    return rec["payload"] if rec else None


def _count_by_scope() -> Dict[str, int]:
    out = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        out[scope] = out.get(scope, 0) + 1
    return out


# ----- request models --------------------------------------------------------

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


# ----- app -------------------------------------------------------------------

app = FastAPI(title="Vera Bot", version="1.0.0")


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": _count_by_scope(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Solo Submission"),
        "team_members": [os.environ.get("TEAM_MEMBER", "candidate")],
        "model": "rule-based-deterministic-composer",
        "approach": (
            "Per-trigger-kind handlers compose deterministic, context-grounded messages. "
            "No LLM in the hot path — every number, name, and date in the output is "
            "traceable to a context field. Reply handler does explicit auto-reply detection, "
            "intent-commit transition, and hostile / opt-out exit."
        ),
        "contact_email": os.environ.get("TEAM_EMAIL", "noreply@example.com"),
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in {"category", "merchant", "customer", "trigger"}:
        return {"accepted": False, "reason": "invalid_scope",
                "details": f"unknown scope '{body.scope}'"}
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {
            "accepted": False,
            "reason": "stale_version",
            "current_version": cur["version"],
        }
    contexts[key] = {"version": body.version, "payload": body.payload}
    log.info("ctx push %s/%s v%d", body.scope, body.context_id, body.version)
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    """
    For each available trigger:
      - Look up trigger, merchant, category, (optional) customer.
      - Skip if we've already sent for that suppression_key this run.
      - Compose; emit one action per (merchant, trigger).
    Cap at 20 actions per the brief.
    """
    actions: List[Dict[str, Any]] = []
    seen_in_this_tick: set = set()  # (merchant_id, conv_id) — at most one each

    for trg_id in body.available_triggers[:50]:  # bounded
        if len(actions) >= 20:
            break

        trigger = _get_ctx("trigger", trg_id)
        if not trigger:
            log.warning("tick: trigger %s not found in context store", trg_id)
            continue

        merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        if not merchant_id:
            log.warning("tick: trigger %s has no merchant_id", trg_id)
            continue

        merchant = _get_ctx("merchant", merchant_id)
        if not merchant:
            log.warning("tick: merchant %s not in store for trigger %s", merchant_id, trg_id)
            continue

        cat_slug = merchant.get("category_slug") or merchant.get("category") or ""
        category = _get_ctx("category", cat_slug) or {}

        customer = None
        cust_id = trigger.get("customer_id")
        if cust_id:
            customer = _get_ctx("customer", cust_id)

        # Suppression — don't double-send the same suppression_key.
        sup_key = trigger.get("suppression_key", f"{trigger.get('kind', 'unknown')}:{merchant_id}")
        if sup_key in sent_suppression_keys:
            continue

        # Per-tick dedup at the (merchant, conversation) level.
        conv_id = f"conv_{merchant_id}_{trg_id}"
        if (merchant_id, conv_id) in seen_in_this_tick:
            continue
        seen_in_this_tick.add((merchant_id, conv_id))

        try:
            composed = compose(category, merchant, trigger, customer)
        except Exception as e:
            log.exception("compose failed for trigger %s: %s", trg_id, e)
            continue

        # Initialize the conversation state so /reply has context.
        conv = rp.get_or_create_conv(conv_id)
        conv.merchant_id = merchant_id
        conv.customer_id = cust_id
        conv.original_trigger_kind = trigger.get("kind")
        conv.original_rationale = composed.get("rationale", "")
        conv.last_bot_body = composed.get("body", "")
        conv.record(
            "vera",
            composed.get("body", ""),
            ts=(body.now or datetime.utcnow().isoformat() + "Z"),
            turn=1,
        )

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": cust_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": composed.get("template_name", "vera_generic_v1"),
            "template_params": composed.get("template_params", []),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

        sent_suppression_keys.add(sup_key)
        suppression_to_conv[sup_key] = conv_id

    log.info("tick: returning %d action(s)", len(actions))
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = rp.get_or_create_conv(body.conversation_id)
    if not conv.merchant_id and body.merchant_id:
        conv.merchant_id = body.merchant_id
    if not conv.customer_id and body.customer_id:
        conv.customer_id = body.customer_id

    # NOTE: we deliberately do NOT record the incoming message before
    # respond() runs. respond() compares the new message against priors
    # (auto-reply detection looks for the same canned text twice). If we
    # recorded first, the new message would always match itself.

    # Look up the live merchant / category / trigger if available.
    merchant = _get_ctx("merchant", conv.merchant_id) if conv.merchant_id else None
    category = None
    if merchant:
        category = _get_ctx("category", merchant.get("category_slug", "")) or None

    # Best-effort find the trigger by walking the conversation_id.
    trigger = None
    if conv.original_trigger_kind:
        # Find a trigger whose kind matches (any).
        for (scope, cid), rec in contexts.items():
            if scope != "trigger":
                continue
            t = rec.get("payload") or {}
            if (t.get("kind") == conv.original_trigger_kind
                    and t.get("merchant_id") == conv.merchant_id):
                trigger = t
                break

    decision = rp.respond(conv, body.message, merchant=merchant,
                          trigger=trigger, category=category)

    # Now record the incoming message into history (after detection).
    conv.record(
        body.from_role,
        body.message,
        ts=(body.received_at or datetime.utcnow().isoformat() + "Z"),
        turn=body.turn_number,
    )

    # Anti-repetition: if we're sending and the body is identical to our
    # previous send in this conv, swap to a wait.
    if decision.get("action") == "send":
        new_body = (decision.get("body") or "").strip()
        if new_body and new_body == (conv.last_bot_body or "").strip():
            decision = {
                "action": "wait",
                "wait_seconds": 1800,
                "rationale": ("Anti-repetition: would have re-sent the same body — "
                              "backing off 30 min instead."),
            }
        else:
            conv.last_bot_body = new_body
            conv.record(
                "vera",
                new_body,
                ts=datetime.utcnow().isoformat() + "Z",
                turn=body.turn_number + 1,
            )

    log.info("reply conv=%s action=%s", body.conversation_id, decision.get("action"))
    return decision


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    sent_suppression_keys.clear()
    suppression_to_conv.clear()
    rp._CONVO_STORE.clear()
    rp._MERCHANT_AUTO_REPLY_STRIKES.clear()
    return {"ok": True, "wiped_at": datetime.utcnow().isoformat() + "Z"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
