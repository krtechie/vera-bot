"""
Vera AI Bot — magicpin AI Challenge Submission
Team: Solo Build
Model: claude-sonnet-4-20250514
Approach: Context-grounded composition with trigger-kind routing, auto-reply detection,
          intent-transition handling, and multi-turn conversation management.
"""

import os
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from composer import compose_message
from conversation import ConversationManager
from context_store import ContextStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Vera AI Bot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TIME = time.time()
store = ContextStore()
conv_manager = ConversationManager()

TEAM_NAME = "VeRA-Elite"
SUBMITTED_AT = "2026-05-02T00:00:00Z"


# ─── Models ────────────────────────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int = 1


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    counts = store.get_counts()
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": ["Solo"],
        "model": "llama-3.3-70b-versatile (Groq free tier)",
        "approach": (
            "Trigger-kind routing → 4-context grounded composition via Groq API. "
            "Auto-reply detection (canned-phrase + repetition fingerprint). "
            "Intent-transition detection (join/confirm/go-ahead keywords → action mode). "
            "Suppression keyed on trigger.suppression_key. "
            "Multi-turn conversation state with graceful exit logic."
        ),
        "contact_email": "submission@vera-ai.example.com",
        "version": "1.0.0",
        "submitted_at": SUBMITTED_AT,
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    result = store.upsert(body.scope, body.context_id, body.version, body.payload)
    if result == "stale":
        current_version = store.get_version(body.scope, body.context_id)
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": current_version},
        )
    if result == "invalid_scope":
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Unknown scope: {body.scope}"},
        )
    ack_id = f"ack_{body.context_id}_v{body.version}"
    stored_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    logger.info(f"Context accepted: scope={body.scope} id={body.context_id} v={body.version}")
    return {"accepted": True, "ack_id": ack_id, "stored_at": stored_at}


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    now_str = body.now

    for trg_id in body.available_triggers:
        # Skip suppressed / already-acted triggers
        if conv_manager.is_suppressed(trg_id):
            logger.info(f"Skipping suppressed trigger: {trg_id}")
            continue

        trg = store.get_payload("trigger", trg_id)
        if not trg:
            logger.warning(f"Trigger not found in store: {trg_id}")
            continue

        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")

        merchant = store.get_payload("merchant", merchant_id) if merchant_id else None
        if not merchant:
            logger.warning(f"Merchant not found for trigger {trg_id}: {merchant_id}")
            continue

        category_slug = merchant.get("category_slug")
        category = store.get_payload("category", category_slug) if category_slug else None
        customer = store.get_payload("customer", customer_id) if customer_id else None

        # Build a new conversation_id for this tick action
        conv_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:8]}"

        # Check existing conversations — don't open a second conversation for same merchant+trigger combo
        existing = conv_manager.find_open_conversation(merchant_id, trg_id)
        if existing:
            logger.info(f"Skipping: open conversation already exists for {merchant_id} + {trg_id}")
            continue

        try:
            result = compose_message(
                category=category or {},
                merchant=merchant,
                trigger=trg,
                customer=customer,
                conversation_history=[],
            )
        except Exception as e:
            logger.error(f"Composition failed for {trg_id}: {e}")
            continue

        if not result or not result.get("body"):
            logger.info(f"Composer returned empty — skipping tick for {trg_id}")
            continue

        # Register conversation
        send_as = result.get("send_as", "vera")
        conv_manager.open_conversation(
            conv_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trg_id,
            suppression_key=result.get("suppression_key", trg.get("suppression_key", "")),
        )
        conv_manager.add_turn(conv_id, role="vera", body=result["body"])

        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": _template_name(trg.get("kind", "generic"), send_as),
            "template_params": _template_params(result, merchant),
            "body": result["body"],
            "cta": result.get("cta", "open_ended"),
            "suppression_key": result.get("suppression_key", trg.get("suppression_key", "")),
            "rationale": result.get("rationale", ""),
        }
        actions.append(action)

        # Suppress this trigger so we don't re-fire it next tick
        conv_manager.suppress(trg_id)

        # Limit to 20 actions per tick (harness cap)
        if len(actions) >= 20:
            break

    logger.info(f"Tick {now_str}: {len(actions)} actions")
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    merchant_id = body.merchant_id
    customer_id = body.customer_id
    message = body.message
    turn_number = body.turn_number

    # Record the incoming message
    conv_manager.add_turn(conv_id, role="merchant" if body.from_role == "merchant" else "customer", body=message)

    # ── Auto-reply detection ────────────────────────────────────────────────
    if _is_auto_reply(message):
        history = conv_manager.get_history(conv_id)
        auto_reply_count = sum(1 for t in history if t["role"] in ("merchant", "customer") and _is_auto_reply(t["body"]))

        if auto_reply_count >= 3:
            logger.info(f"Auto-reply ×3 — ending conversation {conv_id}")
            conv_manager.close_conversation(conv_id)
            return {"action": "end", "rationale": "Auto-reply detected 3× in a row. Owner unreachable via WhatsApp auto-responder. Closing conversation."}

        if auto_reply_count == 2:
            logger.info(f"Auto-reply ×2 — waiting 24h {conv_id}")
            return {"action": "wait", "wait_seconds": 86400, "rationale": "Second consecutive auto-reply. Backing off 24 hours before retry."}

        # First auto-reply — try once with a nudge
        logger.info(f"Auto-reply detected — one-attempt nudge {conv_id}")
        conv_manager.add_turn(conv_id, role="vera", body="Looks like an auto-reply 😊 When the owner sees this, just reply YES to continue.")
        return {
            "action": "send",
            "body": "Looks like an auto-reply 😊 When the owner is free, just reply YES — I'll pick up from there.",
            "cta": "binary_yes_no",
            "rationale": "Detected auto-reply (canned phrase). One-shot nudge to flag for owner, then back off.",
        }

    # ── Hard opt-out detection ──────────────────────────────────────────────
    if _is_hard_no(message):
        logger.info(f"Hard opt-out — ending conversation {conv_id}")
        conv_manager.close_conversation(conv_id)
        conv_manager.suppress_merchant(merchant_id, days=30)
        return {
            "action": "send",
            "body": "Samajh gayi. Main disturb nahi karungi. If anything changes, just say 'Hi Vera' to restart. 🙏",
            "cta": "none",
            "rationale": "Merchant explicitly opted out. Sending a polite exit message and suppressing for 30 days.",
        }

    # ── Intent transition detection ─────────────────────────────────────────
    if _is_intent_action(message):
        logger.info(f"Intent transition detected — switching to action mode {conv_id}")
        return await _handle_intent_action(conv_id, merchant_id, customer_id, message)

    # ── Out-of-scope request ────────────────────────────────────────────────
    if _is_out_of_scope(message):
        # Redirect politely back to the original mission
        conv_info = conv_manager.get_conversation(conv_id)
        return {
            "action": "send",
            "body": "That's a bit outside what I can help with directly — best to check with your CA/team for that. Coming back to where we were: what would you like to do next? Reply YES to proceed or STOP to skip.",
            "cta": "binary_yes_no",
            "rationale": "Out-of-scope question. Politely declined and redirected to original conversation thread.",
        }

    # ── Closed conversation guard ───────────────────────────────────────────
    if conv_manager.is_closed(conv_id):
        return {"action": "end", "rationale": "Conversation already closed. No further sends."}

    # ── Normal reply handling ───────────────────────────────────────────────
    conv_info = conv_manager.get_conversation(conv_id)
    trigger_id = conv_info.get("trigger_id") if conv_info else None
    trg = store.get_payload("trigger", trigger_id) if trigger_id else {}
    merchant = store.get_payload("merchant", merchant_id) if merchant_id else {}
    category_slug = merchant.get("category_slug") if merchant else None
    category = store.get_payload("category", category_slug) if category_slug else {}
    customer = store.get_payload("customer", customer_id) if customer_id else None
    history = conv_manager.get_history(conv_id)

    # Max turns guard (5 turns deep)
    vera_turns = [t for t in history if t["role"] == "vera"]
    if len(vera_turns) >= 5:
        conv_manager.close_conversation(conv_id)
        return {"action": "end", "rationale": "Reached maximum conversation depth (5 turns). Closing gracefully."}

    try:
        result = compose_message(
            category=category,
            merchant=merchant,
            trigger=trg,
            customer=customer,
            conversation_history=history,
        )
    except Exception as e:
        logger.error(f"Reply composition failed: {e}")
        return {
            "action": "send",
            "body": "Main ek second mein wapas aati hoon — ek chhoti si technical dikkat aa gayi. 🙏",
            "cta": "none",
            "rationale": "Composition error — fallback holding message.",
        }

    if not result or not result.get("body"):
        conv_manager.close_conversation(conv_id)
        return {"action": "end", "rationale": "No further action needed at this point."}

    # Anti-repetition check
    body_text = result["body"]
    if conv_manager.is_repeat(conv_id, body_text):
        logger.warning(f"Repeat body detected — skipping send {conv_id}")
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Would have repeated prior message. Backing off 1 hour."}

    conv_manager.add_turn(conv_id, role="vera", body=body_text)
    return {
        "action": "send",
        "body": body_text,
        "cta": result.get("cta", "open_ended"),
        "rationale": result.get("rationale", ""),
    }


@app.post("/v1/teardown")
async def teardown():
    store.clear()
    conv_manager.clear()
    logger.info("Teardown complete — all state wiped.")
    return {"ok": True}


# ─── Helpers ───────────────────────────────────────────────────────────────────

AUTO_REPLY_PHRASES = [
    "thank you for contacting",
    "our team will respond shortly",
    "automated message",
    "we have received your message",
    "we'll get back to you",
    "this is an automated",
    "auto-reply",
    "aapki jaankari ke liye bahut-bahut shukriya",
    "aapki madad ke liye shukriya",
    "main ek automated assistant hoon",
    "shukriya. main aapke sandesh ko team tak pahuncha dungi",
]

HARD_NO_PHRASES = [
    "not interested",
    "stop messaging",
    "stop contacting",
    "remove me",
    "do not message",
    "opt out",
    "unsubscribe",
    "band karo",
    "mat karo",
    "nahi chahiye",
    "boring",
    "spam",
    "why are you bothering",
    "stop sending these",
    "useless",
]

INTENT_ACTION_PHRASES = [
    "let's do it",
    "let's go",
    "ok go ahead",
    "yes go ahead",
    "please proceed",
    "haan karo",
    "kar do",
    "yes please do",
    "do it",
    "confirm",
    "great, proceed",
    "sounds good, proceed",
    "go for it",
    "chaliye shuru karte hain",
    "aage badho",
    "what's next",
    "ok let's start",
]

OUT_OF_SCOPE_KEYWORDS = [
    "gst", "income tax", "itr", "loan", "insurance", "legal", "court", "police",
    "passport", "visa", "election", "help me book", "book a flight", "hotel",
]


def _is_auto_reply(msg: str) -> bool:
    m = msg.lower().strip()
    return any(phrase in m for phrase in AUTO_REPLY_PHRASES)


def _is_hard_no(msg: str) -> bool:
    m = msg.lower()
    return any(phrase in m for phrase in HARD_NO_PHRASES)


def _is_intent_action(msg: str) -> bool:
    m = msg.lower()
    return any(phrase in m for phrase in INTENT_ACTION_PHRASES)


def _is_out_of_scope(msg: str) -> bool:
    m = msg.lower()
    return any(kw in m for kw in OUT_OF_SCOPE_KEYWORDS)


async def _handle_intent_action(conv_id: str, merchant_id: Optional[str], customer_id: Optional[str], message: str) -> dict:
    """Merchant said 'yes let's do it' — switch from qualifying to action mode."""
    merchant = store.get_payload("merchant", merchant_id) if merchant_id else {}
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
    offers = merchant.get("offers", [])
    active_offers = [o for o in offers if o.get("status") == "active"]
    offer_str = active_offers[0]["title"] if active_offers else "your current offer"
    signals = merchant.get("signals", [])

    action_lines = []
    if "stale_posts" in " ".join(signals):
        action_lines.append("Draft 3 fresh Google posts")
    if "ctr_below_peer_median" in " ".join(signals):
        action_lines.append(f"Optimize your listing with '{offer_str}'")
    if not action_lines:
        action_lines.append(f"Set up '{offer_str}' as a featured campaign")

    body = (
        f"Perfect! Starting now — {action_lines[0]}. "
        f"I'll have a draft ready in 60 seconds. "
        f"Reply CONFIRM to publish, or I can show you a preview first."
    )
    conv_manager.add_turn(conv_id, role="vera", body=body)
    return {
        "action": "send",
        "body": body,
        "cta": "binary_confirm_cancel",
        "rationale": "Merchant explicitly committed to action. Switched from qualifying to execution mode with one concrete deliverable and a binary CTA.",
    }


def _template_name(kind: str, send_as: str) -> str:
    if send_as == "merchant_on_behalf":
        return "merchant_recall_reminder_v1"
    mapping = {
        "research_digest": "vera_research_digest_v1",
        "regulation_change": "vera_compliance_alert_v1",
        "perf_spike": "vera_performance_spike_v1",
        "perf_dip": "vera_performance_dip_v1",
        "recall_due": "vera_recall_reminder_v1",
        "festival_upcoming": "vera_festival_campaign_v1",
        "dormant_with_vera": "vera_reengagement_v1",
        "milestone_reached": "vera_milestone_v1",
        "competitor_opened": "vera_competitor_alert_v1",
        "curious_ask_due": "vera_curious_ask_v1",
        "category_trend_movement": "vera_trend_signal_v1",
    }
    return mapping.get(kind, "vera_generic_v1")


def _template_params(result: dict, merchant: dict) -> list[str]:
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
    body = result.get("body", "")
    # Split body into roughly 3 params
    words = body.split()
    chunk = max(1, len(words) // 3)
    p1 = " ".join(words[:chunk])
    p2 = " ".join(words[chunk:chunk*2])
    p3 = " ".join(words[chunk*2:])
    return [name, p1, p2, p3]
