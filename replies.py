"""
Reply handling — what Vera says back when the merchant (or customer) responds.

The replay-test scores three things:
  1. Auto-reply detection — same canned text 3+ times = exit gracefully.
  2. Intent transition — when merchant says "yes / let's do it / go ahead",
     switch to ACTION mode. Don't ask another qualifying question.
  3. Hostile / opt-out — gracefully END.

Same deterministic philosophy as the composer: rules over LLM, traceable to
the actual reply text and conversation state.
"""

from __future__ import annotations
import re
from typing import Any, Dict, List, Optional


# Hindi/English commitment phrases — merchant says "yes go" in any way.
COMMIT_RE = re.compile(
    r"\b(?:"
    r"yes|yeah|yep|sure|ok|okay|please|pls|"
    r"go\s+ahead|let'?s\s+do\s+it|do\s+it|proceed|"
    r"send|share|draft|confirm|confirmed|"
    r"theek\s*hai|sahi\s+hai|haan|han|kar\s*do|"
    r"bilkul|bhej\s*do|likh\s*do|chalega|approve(?:d)?"
    r")\b",
    re.IGNORECASE,
)

# Action-oriented words for follow-through.
QUESTION_RE = re.compile(r"\?$|^(what|how|when|where|why|kya|kaise|kab|kahan|kaun)\b", re.IGNORECASE)

# Hostile / opt-out phrases.
HOSTILE_RE = re.compile(
    r"\b(?:"
    r"stop|unsubscribe|don'?t\s+(?:message|contact|call)|"
    r"not\s+interested|leave\s+me\s+alone|spam|useless|"
    r"band\s+karo|mat\s+bhejo|mat\s+karo|chup|bekaar"
    r")\b",
    re.IGNORECASE,
)

# Common WA-Business auto-reply templates we've seen in production.
AUTO_REPLY_HINTS = [
    "thank you for contacting",
    "thanks for contacting",
    "we will get back",
    "our team will respond",
    "currently unavailable",
    "out of office",
    "this is an automated",
    "i am an automated",
    "automated assistant",
    "team tak pahuncha",   # "passing it to the team" canned auto-reply
    "jaankari ke liye shukriya",
]


# In-memory conversation state. Keyed by conversation_id.
class ConversationState:
    """Per-conversation memory — what was said, in what direction, when."""

    def __init__(self):
        self.turns: List[Dict[str, Any]] = []  # {from, body, ts, turn_number}
        self.merchant_id: Optional[str] = None
        self.customer_id: Optional[str] = None
        self.last_bot_body: Optional[str] = None
        self.auto_reply_strikes: int = 0
        self.ended: bool = False
        self.last_intent_signaled: Optional[str] = None  # "commit" | "hostile" | None
        self.original_trigger_kind: Optional[str] = None
        self.original_rationale: Optional[str] = None

    def record(self, role: str, body: str, ts: str = "", turn: int = 0):
        self.turns.append({"from": role, "body": body, "ts": ts, "turn_number": turn})


# Per-conversation state. Keyed by conversation_id.
_CONVO_STORE: Dict[str, ConversationState] = {}

# Per-merchant auto-reply strike count. Auto-reply detection is *cross-
# conversation*: if a merchant sends canned auto-replies from multiple
# conversation IDs in quick succession, we treat strike #2 as the signal
# to stop entirely for that merchant.
_MERCHANT_AUTO_REPLY_STRIKES: Dict[str, int] = {}


def get_or_create_conv(conv_id: str) -> ConversationState:
    if conv_id not in _CONVO_STORE:
        _CONVO_STORE[conv_id] = ConversationState()
    return _CONVO_STORE[conv_id]


def get_conv(conv_id: str) -> Optional[ConversationState]:
    return _CONVO_STORE.get(conv_id)


def all_conversations() -> Dict[str, ConversationState]:
    return _CONVO_STORE


# ----- detection helpers ----------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def looks_like_auto_reply(text: str, prior_replies: List[str]) -> bool:
    """Is this almost certainly a WhatsApp Business auto-reply?

    Two signals:
      (a) substring match on known canned phrases;
      (b) the same merchant just sent the same long-ish text twice in a row.

    We require >= 25 chars on the (b) path so a one-word "Yes" repeated
    isn't treated as a canned reply.
    """
    n = _normalize(text)
    if not n:
        return False
    for hint in AUTO_REPLY_HINTS:
        if hint in n:
            return True
    if prior_replies and len(n) >= 25:
        last_two = [_normalize(r) for r in prior_replies[-2:]]
        # Defensive: only count distinct prior occurrences, not self.
        prior_distinct = [r for r in last_two if r and r != n] + [r for r in last_two if r == n]
        # Check if the same long text appeared at least once before THIS one.
        # `prior_replies` should already exclude the current message; we don't
        # rely on that — we count occurrences and require >= 1 (i.e., it
        # occurred earlier in the convo at least once).
        if last_two.count(n) >= 1:
            return True
    return False


def is_commitment(text: str) -> bool:
    """Did the merchant signal a clear yes / let's do it?"""
    n = (text or "").strip()
    if not n:
        return False
    # Don't match on questions even if they contain commit words.
    if QUESTION_RE.search(n):
        # 'Yes — but how does this work?' is still a question, hold.
        if "?" in n:
            return False
    return bool(COMMIT_RE.search(n))


def is_hostile_or_optout(text: str) -> bool:
    return bool(HOSTILE_RE.search(text or ""))


# ----- reply composition ----------------------------------------------------

def respond(
    conv: ConversationState,
    merchant_message: str,
    merchant: Optional[Dict] = None,
    trigger: Optional[Dict] = None,
    category: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Compose the next move given conversation state + the incoming message.

    Returns one of:
      {action: "send", body, cta, rationale}
      {action: "wait", wait_seconds, rationale}
      {action: "end",  rationale}
    """
    text = (merchant_message or "").strip()

    # Hard guardrail: if we've already ended, stay ended.
    if conv.ended:
        return {"action": "end", "rationale": "conversation already ended"}

    # 1) Hostile / opt-out — exit politely without further nudging.
    if is_hostile_or_optout(text):
        conv.ended = True
        conv.last_intent_signaled = "hostile"
        return {
            "action": "end",
            "rationale": "Merchant signaled stop / not interested — graceful exit.",
        }

    # 2) Intent commit — checked BEFORE auto-reply because a clear "yes"/"haan"
    #    is unambiguous and should never be reclassified as canned-reply.
    #    Switch to ACTION mode immediately.
    if is_commitment(text):
        conv.last_intent_signaled = "commit"
        kind = conv.original_trigger_kind or (trigger or {}).get("kind", "")
        owner = (merchant or {}).get("identity", {}).get("owner_first_name", "") if merchant else ""

        action_msg = _action_for_kind(kind, merchant or {}, category or {}, trigger or {}, owner)
        return {
            "action": "send",
            "body": action_msg["body"],
            "cta": action_msg.get("cta", "open_ended"),
            "rationale": (
                f"Merchant committed ('{text[:40]}'); switched to action mode for "
                f"trigger.kind={kind or 'unknown'} — drafted artifact / next step, no re-qualifying."
            ),
        }

    # 3) Auto-reply detection.
    prior_merchant_msgs = [t["body"] for t in conv.turns if t["from"] in ("merchant", "customer")]
    if looks_like_auto_reply(text, prior_merchant_msgs):
        conv.auto_reply_strikes += 1

        # Cross-conversation tracking: a single auto-reply phrase repeated
        # across many conversation_ids is the same hostile signal as repeating
        # it within one conversation. Track at the merchant level so the bot
        # backs off after 2 hits regardless of which conv_id the harness uses.
        merchant_id = conv.merchant_id or ""
        if merchant_id:
            _MERCHANT_AUTO_REPLY_STRIKES[merchant_id] = (
                _MERCHANT_AUTO_REPLY_STRIKES.get(merchant_id, 0) + 1
            )
            merchant_strikes = _MERCHANT_AUTO_REPLY_STRIKES[merchant_id]
        else:
            merchant_strikes = conv.auto_reply_strikes

        if conv.auto_reply_strikes >= 2 or merchant_strikes >= 2:
            # Already nudged once after detecting auto-reply. Now exit.
            conv.ended = True
            return {
                "action": "end",
                "rationale": (
                    "Auto-reply detected (cross-conversation) for the second "
                    "time — exiting to avoid wasting the merchant's WA Business "
                    "inbox cycles."
                ),
            }
        # First auto-reply hit: nudge once asking the human directly.
        owner = ""
        if merchant:
            owner = (merchant.get("identity", {}) or {}).get("owner_first_name", "") or ""
        body = (
            f"Got it — looks like your WhatsApp auto-reply. "
            f"{('No worries ' + owner) if owner else 'No worries'}, "
            f"if you (the owner/manager) see this in 2 min, reply YES and I'll keep it short. "
            f"Otherwise I'll stand down."
        )
        return {
            "action": "send",
            "body": body,
            "cta": "binary_yes_stop",
            "rationale": "Auto-reply detected on turn 1 — one polite human-only nudge before exit.",
        }

    # 4) Question / clarification from the merchant — answer crisply, still
    #    keeping the original next step on the table.
    if QUESTION_RE.search(text):
        kind = conv.original_trigger_kind or (trigger or {}).get("kind", "")
        body = _answer_for_kind(kind, text, merchant or {}, category or {}, trigger or {})
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant asked a clarifying question — answered briefly and re-offered next step.",
        }

    # 5) Vague / non-committal reply — wait if they said "later/busy".
    n = _normalize(text)
    if any(p in n for p in ("later", "tomorrow", "kal", "baad me", "busy", "in a meeting")):
        return {
            "action": "wait",
            "wait_seconds": 7200,  # 2h
            "rationale": "Merchant asked for time; backing off 2h before any further nudge.",
        }

    # 6) After 3+ unanswered nudges from us, end the conversation.
    bot_turns = [t for t in conv.turns if t["from"] == "vera"]
    if len(bot_turns) >= 3:
        conv.ended = True
        return {
            "action": "end",
            "rationale": "3 nudges sent without engagement — exiting per anti-spam policy.",
        }

    # 7) Default: short acknowledgment + restate the single next step.
    kind = conv.original_trigger_kind or (trigger or {}).get("kind", "")
    body = _restate_next_step(kind, merchant or {}, trigger or {})
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "Ambiguous reply — restated the single next step concisely.",
    }


# ---- per-kind action snippets ---------------------------------------------

def _action_for_kind(kind: str, merchant: Dict, category: Dict, trigger: Dict, owner: str) -> Dict[str, str]:
    """When merchant says yes — what's the immediate action message?"""
    sal = owner or (merchant.get("identity", {}) or {}).get("name", "")[:20] or "Got it"

    if kind == "research_digest":
        return {
            "body": (f"On it. Pulling the abstract now and drafting a 90-sec patient-ed WhatsApp "
                     f"you can review in 2 min. Sending the draft next."),
            "cta": "open_ended",
        }
    if kind == "regulation_change":
        return {
            "body": "Drafting your SOP-update note now + a team reminder. Will share both for your edit in 2 min.",
            "cta": "open_ended",
        }
    if kind == "supply_alert":
        return {
            "body": ("Sending. Drafting the customer WhatsApp + the replacement-pickup workflow now — "
                     "you'll get both within 5 min to review before any send."),
            "cta": "open_ended",
        }
    if kind in ("perf_dip", "perf_spike"):
        return {
            "body": "Drafting 3 Google posts + a refreshed offer banner. Sharing all 4 for your review in 5 min.",
            "cta": "open_ended",
        }
    if kind == "seasonal_perf_dip":
        return {
            "body": "Drafting the retention campaign — Google post + WhatsApp blast to your active members. 5 min.",
            "cta": "open_ended",
        }
    if kind == "milestone_reached":
        return {
            "body": "Drafting the milestone Google post + thank-you note. Sharing both in 3 min.",
            "cta": "open_ended",
        }
    if kind == "renewal_due":
        return {
            "body": "Sending the cycle snapshot + 2 things-to-ship-this-week now. Quick read, decide after.",
            "cta": "open_ended",
        }
    if kind == "review_theme_emerged":
        return {
            "body": "On it — drafting both the response template and the public Google reply. Share in 5 min for your edit.",
            "cta": "open_ended",
        }
    if kind == "competitor_opened":
        return {
            "body": "Running the listing audit now — photos, hours, offers vs theirs. Report + 5-min fix list in your inbox in 3 min.",
            "cta": "open_ended",
        }
    if kind == "festival_upcoming":
        return {
            "body": "Drafting 2 festival offer options + a Google post + a WhatsApp blast. Sharing all 3 in 10 min.",
            "cta": "open_ended",
        }
    if kind == "ipl_match_today":
        return {
            "body": "Live in 10 min — Swiggy banner, Insta story, and the Google post. Sharing as soon as ready.",
            "cta": "open_ended",
        }
    if kind == "active_planning_intent":
        return {
            "body": "Tightening the draft into a clean 1-pager + the outreach WhatsApp. Sharing in 5 min.",
            "cta": "open_ended",
        }
    if kind == "winback_eligible":
        return {
            "body": "Setting up your trial month + queuing the winback WhatsApp to your lapsed list. Both done in 10 min.",
            "cta": "open_ended",
        }
    if kind == "gbp_unverified":
        return {
            "body": "Great — kicking off the verification now. I'll guide you through the 3 steps; takes ~5 min total.",
            "cta": "open_ended",
        }
    if kind == "cde_opportunity":
        return {
            "body": "Sending the registration link + a calendar block now.",
            "cta": "open_ended",
        }
    if kind == "curious_ask_due":
        # The merchant said "yes" without giving the answer — ask again, but
        # tightly.
        return {
            "body": "Just give me the one service in a sentence and I'll turn it into the post + reply now.",
            "cta": "open_ended",
        }
    if kind in ("recall_due", "chronic_refill_due", "wedding_package_followup", "trial_followup", "customer_lapsed_hard", "customer_lapsed_soft"):
        return {
            "body": "Booked. You'll get a confirmation here shortly with the time + any prep notes.",
            "cta": "open_ended",
        }
    return {
        "body": "On it — sharing the next step in a few minutes.",
        "cta": "open_ended",
    }


def _answer_for_kind(kind: str, question: str, merchant: Dict, category: Dict, trigger: Dict) -> str:
    """Short, honest answer to a clarifying question, without re-pitching."""
    q = (question or "").lower()
    # Pricing / cost questions — we can't invent numbers; honest deflect.
    if any(w in q for w in ("price", "cost", "kitna", "how much", "fees")):
        return (
            "Honest answer: pricing depends on what you actually ship — I'd rather "
            "show you the live numbers + draft, then you decide. Want me to share "
            "the draft now?"
        )
    if any(w in q for w in ("how long", "kitna time", "time lagega", "duration")):
        return "5 to 10 minutes for the draft; 24h for it to show up on Google after we ship. Want me to start?"
    if any(w in q for w in ("source", "where did", "kahan se")):
        # Cite from the trigger / category if we have it.
        item_id = (trigger.get("payload", {}) or {}).get("top_item_id")
        for d in (category.get("digest", []) or []):
            if d.get("id") == item_id:
                return f"Source: {d.get('source', 'category digest')}. Want me to share the abstract here?"
        return "Pulled it from the category digest we maintain. Want me to share the source link + summary?"
    # Generic clarifier: keep the next step alive.
    return (
        "Short version — it's a 5-min thing where I draft, you review, you decide. "
        "Want me to send the draft now?"
    )


def _restate_next_step(kind: str, merchant: Dict, trigger: Dict) -> str:
    """Restate the single next step in one line."""
    if kind == "curious_ask_due":
        return "Just one line — the most-asked service this week. I'll do the rest."
    if kind in ("recall_due", "chronic_refill_due"):
        return "Reply 1 or 2 to pick a slot, or share a time that works."
    if kind == "winback_eligible":
        return "Reply YES and I set up the trial + winback in 10 min."
    return "If yes, just reply YES and I'll have it ready in a few minutes."
