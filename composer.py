"""
Vera Composer — the intelligence layer.

Routes each trigger.kind to a specialized prompt strategy, then calls Claude API
to produce a grounded, merchant-specific message.

Design principles:
- Every message must have ONE primary signal (the trigger) driving it.
- Use real numbers from merchant context (CTR, views, offers, etc.).
- Match language preference (hi-en mix when merchant.identity.languages includes "hi").
- Single CTA per message; binary for action triggers, open-ended for info triggers.
- Never fabricate data not present in the provided contexts.
"""

import os
import json
import logging
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"

# Trigger-kind → prompt strategy mapping
TRIGGER_STRATEGIES = {
    "research_digest": "research",
    "regulation_change": "compliance",
    "category_research_digest_release": "research",
    "perf_spike": "performance_spike",
    "perf_dip": "performance_dip",
    "milestone_reached": "milestone",
    "dormant_with_vera": "reengagement",
    "recall_due": "recall",
    "customer_lapsed_soft": "recall",
    "festival_upcoming": "festival",
    "weather_heatwave": "weather_event",
    "local_news_event": "local_event",
    "competitor_opened": "competitor",
    "category_trend_movement": "trend",
    "review_theme_emerged": "review_theme",
    "curious_ask_due": "curious_ask",
    "scheduled_recurring": "curious_ask",
    "appointment_tomorrow": "appointment",
    "active_planning_intent": "action_mode",
    "ipl_match_today": "local_event",
    "bridal_followup": "recall",
}

# Voice profiles by category
VOICE_GUIDES = {
    "dentists": "peer/collegial clinical tone. Use technical vocab (fluoride varnish, caries, OPG, RCT). Never say 'guaranteed' or 'cure'. Salute as 'Dr. {first_name}'.",
    "salons": "warm, practical, operator-to-operator. Use category terms (bridal, keratin, threading). Conversational, friendly. Address by first name.",
    "restaurants": "busy-practical, operator voice. Use 'covers', 'footfall', 'delivery radius'. Direct. Address by first name.",
    "gyms": "energetic but disciplined. Use 'members', 'batch', 'retention'. No hype like 'shred in 7 days'. Address by first name.",
    "pharmacies": "trustworthy, precise. Use 'patients', 'refill', 'compliance'. Never 'miracle cure'. Address as formal name.",
}


def compose_message(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    conversation_history: list[dict],
) -> Optional[dict]:
    """
    Main composition entry point.
    Returns dict with: body, cta, send_as, suppression_key, rationale
    """
    if not merchant or not trigger:
        return None

    trigger_kind = trigger.get("kind", "generic")
    strategy = TRIGGER_STRATEGIES.get(trigger_kind, "generic")

    # Resolve digest item if trigger references one
    digest_item = _resolve_digest(trigger, category)

    # Build the system prompt
    system_prompt = _build_system_prompt(strategy, category, merchant, trigger, customer, digest_item)

    # Build the user prompt
    user_prompt = _build_user_prompt(strategy, category, merchant, trigger, customer, digest_item, conversation_history)

    # Call Claude
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            temperature=0,  # deterministic
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return _fallback_compose(merchant, trigger, customer)

    # Parse JSON response
    result = _parse_response(raw)
    if not result:
        return _fallback_compose(merchant, trigger, customer)

    # Ensure suppression_key is set
    if not result.get("suppression_key"):
        result["suppression_key"] = trigger.get("suppression_key", f"{trigger_kind}:{merchant.get('merchant_id', 'unknown')}")

    # Determine send_as
    result["send_as"] = "merchant_on_behalf" if customer else "vera"

    return result


# ─── System Prompt Builder ─────────────────────────────────────────────────────

def _build_system_prompt(strategy: str, category: dict, merchant: dict, trigger: dict,
                          customer: Optional[dict], digest_item: Optional[dict]) -> str:
    slug = category.get("slug", "")
    voice_guide = VOICE_GUIDES.get(slug, "warm, professional, direct.")
    voice = category.get("voice", {})
    taboos = voice.get("vocab_taboo", voice.get("vocab_taboo_words", []))
    taboo_str = ", ".join(f'"{t}"' for t in taboos) if taboos else "none listed"

    lang_pref = _get_language(merchant, customer)

    return f"""You are Vera, magicpin's AI merchant growth assistant. You compose WhatsApp messages for Indian merchants.

CATEGORY: {slug or "unknown"}
VOICE GUIDE: {voice_guide}
FORBIDDEN WORDS: {taboo_str}
LANGUAGE: {lang_pref}

COMPOSITION RULES (non-negotiable):
1. Ground EVERY claim in the provided context. Do NOT invent numbers, offers, competitor names, or research citations not given to you.
2. Use ONE primary compulsion lever: specificity (real numbers/dates), loss aversion, social proof, curiosity, effort externalization, or asking the merchant.
3. ONE primary CTA only. Binary (YES/STOP or CONFIRM/CANCEL) for action triggers. Open-ended question for information triggers. No CTA for pure-information triggers.
4. Body must be concise — conversational WhatsApp length. No long preambles. No re-introductions after the first message.
5. End with the CTA in the last sentence.
6. Do NOT include URLs.
7. If language is "hi-en mix" or merchant speaks Hindi, naturally code-mix Hindi and English the way Indians message on WhatsApp. Not formal Hindi — conversational Hinglish.
8. NEVER use the forbidden words for this category.
9. Trigger is the REASON this message exists. Make that reason clear in the message.
10. Rationale must explain: which signal you chose, which compulsion lever, and why this is the right moment.

STRATEGY: {strategy}

Return ONLY a valid JSON object with these exact keys:
{{
  "body": "<the WhatsApp message body>",
  "cta": "<open_ended|binary_yes_no|binary_confirm_cancel|none>",
  "suppression_key": "<a dedup key>",
  "rationale": "<1-2 sentence explanation of signal, lever, and timing>"
}}"""


# ─── User Prompt Builder ───────────────────────────────────────────────────────

def _build_user_prompt(strategy: str, category: dict, merchant: dict, trigger: dict,
                        customer: Optional[dict], digest_item: Optional[dict],
                        conversation_history: list[dict]) -> str:

    merchant_id = merchant.get("merchant_id", "")
    identity = merchant.get("identity", {})
    name = identity.get("owner_first_name") or identity.get("name", "")
    city = identity.get("city", "")
    locality = identity.get("locality", "")
    verified = identity.get("verified", False)
    languages = identity.get("languages", ["en"])

    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)
    ctr = perf.get("ctr", 0)
    directions = perf.get("directions", 0)
    delta = perf.get("delta_7d", {})
    views_delta = delta.get("views_pct", 0)
    calls_delta = delta.get("calls_pct", 0)

    offers = merchant.get("offers", [])
    active_offers = [o for o in offers if o.get("status") == "active"]
    offers_str = "; ".join(o["title"] for o in active_offers) if active_offers else "no active offers"

    signals = merchant.get("signals", [])
    signals_str = ", ".join(signals) if signals else "none"

    cust_agg = merchant.get("customer_aggregate", {})
    lapsed = cust_agg.get("lapsed_180d_plus", 0)
    retention = cust_agg.get("retention_6mo_pct", 0)
    high_risk = cust_agg.get("high_risk_adult_count", 0)

    sub = merchant.get("subscription", {})
    sub_status = sub.get("status", "unknown")
    sub_days = sub.get("days_remaining", 0)
    sub_plan = sub.get("plan", "")

    cat_peer = category.get("peer_stats", {})
    peer_ctr = cat_peer.get("avg_ctr", 0)
    peer_rating = cat_peer.get("avg_rating", 0)

    seasonal_beats = category.get("seasonal_beats", [])
    seasonal_str = "; ".join(f"{b.get('month_range', '')}: {b.get('note', '')}" for b in seasonal_beats[:2])

    trend_signals = category.get("trend_signals", [])
    trend_str = "; ".join(f"{t.get('query', '')}: +{int(t.get('delta_yoy', 0)*100)}% YoY" for t in trend_signals[:2])

    review_themes = merchant.get("review_themes", [])
    review_str = "; ".join(f"{r['theme']}({r['sentiment']}): {r.get('common_quote', '')}" for r in review_themes[:2])

    conv_hist = merchant.get("conversation_history", [])
    last_vera = next((t["body"] for t in reversed(conv_hist) if t.get("from") == "vera"), None)
    last_merchant = next((t["body"] for t in reversed(conv_hist) if t.get("from") == "merchant"), None)

    trigger_kind = trigger.get("kind", "generic")
    trigger_payload = trigger.get("payload", {})
    trigger_urgency = trigger.get("urgency", 1)
    suppression_key = trigger.get("suppression_key", "")

    # Build digest section
    digest_section = ""
    if digest_item:
        digest_section = f"""
DIGEST ITEM (this is the trigger's content):
- Title: {digest_item.get('title', '')}
- Source: {digest_item.get('source', '')}
- Summary: {digest_item.get('summary', '')}
- Trial N: {digest_item.get('trial_n', 'N/A')}
- Patient segment: {digest_item.get('patient_segment', 'N/A')}
- Kind: {digest_item.get('kind', '')}
"""

    # Customer section (if present)
    customer_section = ""
    if customer:
        cid = customer.get("identity", {})
        crel = customer.get("relationship", {})
        cstate = customer.get("state", "unknown")
        cpref = customer.get("preferences", {})
        cname = cid.get("name", "the customer")
        clang = cid.get("language_pref", "en")
        last_visit = crel.get("last_visit", "unknown")
        visits = crel.get("visits_total", 0)
        services = crel.get("services_received", [])
        preferred_slot = cpref.get("preferred_slots", "any time")
        customer_section = f"""
CUSTOMER CONTEXT (send as merchant_on_behalf):
- Name: {cname}
- Language preference: {clang}
- Last visit: {last_visit}
- Total visits: {visits}
- Services received: {', '.join(services)}
- State: {cstate}
- Preferred slots: {preferred_slot}
- Consent scope: {', '.join(customer.get('consent', {}).get('scope', []))}
"""

    # Conversation history section
    hist_section = ""
    if conversation_history:
        recent = conversation_history[-4:]  # last 4 turns
        hist_section = "\nRECENT CONVERSATION:\n" + "\n".join(
            f"[{t['role'].upper()}]: {t['body']}" for t in recent
        )
    elif last_vera or last_merchant:
        hist_section = f"\nPRIOR VERA MESSAGE: {last_vera or '(none)'}\nMERCHANT LAST REPLY: {last_merchant or '(none)'}"

    strategy_hint = _strategy_hint(strategy, trigger_kind, trigger_payload, merchant)

    return f"""MERCHANT CONTEXT:
- ID: {merchant_id}
- Name: {name}
- Location: {locality}, {city}
- Verified: {verified}
- Languages: {', '.join(languages)}
- Subscription: {sub_plan} plan, {sub_status}, {sub_days} days remaining

PERFORMANCE (last 30 days):
- Views: {views} ({'+' if views_delta >= 0 else ''}{int(views_delta*100)}% vs last week)
- Calls: {calls} ({'+' if calls_delta >= 0 else ''}{int(calls_delta*100)}% vs last week)
- Directions: {directions}
- CTR: {ctr:.3f} (peer median: {peer_ctr:.3f})

ACTIVE OFFERS: {offers_str}
SIGNALS: {signals_str}
CUSTOMER AGGREGATE: {lapsed} lapsed 180d+, {int(retention*100)}% 6-month retention{f', {high_risk} high-risk adults' if high_risk else ''}

CATEGORY BENCHMARKS:
- Peer avg CTR: {peer_ctr:.3f}, peer avg rating: {peer_rating}
- Seasonal beats: {seasonal_str or 'none'}
- Trend signals: {trend_str or 'none'}

REVIEW THEMES: {review_str or 'none'}
{digest_section}
TRIGGER:
- Kind: {trigger_kind}
- Urgency: {trigger_urgency}/5
- Payload: {json.dumps(trigger_payload, ensure_ascii=False)}
- Suppression key: {suppression_key}
{customer_section}
{hist_section}

STRATEGY HINT: {strategy_hint}

Now compose the perfect Vera message for this trigger. Remember: ground every fact in the context above. Return only JSON."""


def _strategy_hint(strategy: str, kind: str, payload: dict, merchant: dict) -> str:
    perf = merchant.get("performance", {})
    ctr = perf.get("ctr", 0)
    cat_peer = {}  # will be resolved at call site if needed

    hints = {
        "research": "Lead with the research finding (specific numbers + source). Ask if merchant wants to act on it. Use curiosity lever.",
        "compliance": "Lead with the regulatory deadline. Frame as 'heads up, not a scare'. Name the specific change. Binary yes/no to acknowledge.",
        "performance_spike": f"Views spiked — call it out with the number ({perf.get('views', 0)} views). Ask 'do you know why?' — curiosity lever. Suggest one thing to capitalize on it.",
        "performance_dip": f"CTR {ctr:.3f} is below peer median. Don't say 'you're failing' — say 'here's a gap I noticed'. Loss aversion + one concrete fix. Binary CTA.",
        "milestone": "Celebrate concretely. Then turn it into a next-step action — \"now that you have X reviews, here's what to do with them.\"",
        "reengagement": "Merchant hasn't talked to Vera in a while. Light touch — short curious-ask. Not a hard sell. Ask what's been top of mind.",
        "recall": "Patient-facing or merchant-facing recall. Be warm and specific — name, time since last visit, slot options. Preference-matched CTA.",
        "festival": "Festival in N days. Don't just say 'run a discount'. Suggest the specific format (service+price) that works for this category. Time-bound urgency.",
        "weather_event": "Weather is a real event affecting footfall. Give a contrarian or useful insight. What should the merchant do differently today?",
        "local_event": "Local event = opportunity or risk. Be specific about what it means for THIS merchant's footfall/sales. Concrete recommendation.",
        "competitor": "New competitor nearby. Use loss aversion carefully — don't be alarmist. One concrete defensive action the merchant can take TODAY.",
        "trend": "Search trend is rising in their category/city. Frame as an opportunity: 'here's who's searching.' Suggest one offer or post to capture it.",
        "review_theme": "Reviews surfaced a pattern. Be constructive — 'I noticed X theme in your last few reviews; here's how to address it in one message.'",
        "curious_ask": "Weekly knowledge exchange. Ask ONE thing about the merchant's business that Vera could help with. Low-stakes, conversational. No hard sell.",
        "appointment": "Appointment is tomorrow. Prep the merchant. Offer to draft a reminder or confirmation message they can send.",
        "action_mode": "Merchant has said they want to proceed. Skip qualifying. Go straight to execution. Present the concrete deliverable with a binary CONFIRM CTA.",
        "generic": "Use the most specific signal from merchant context. Anchor on a real number. Single clear ask.",
    }
    return hints.get(strategy, hints["generic"])


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_digest(trigger: dict, category: dict) -> Optional[dict]:
    """Find the digest item referenced by the trigger."""
    payload = trigger.get("payload", {})
    top_item_id = payload.get("top_item_id")
    if not top_item_id:
        return None
    digests = category.get("digest", [])
    for item in digests:
        if item.get("id") == top_item_id:
            return item
    return None


def _get_language(merchant: dict, customer: Optional[dict]) -> str:
    if customer:
        lang = customer.get("identity", {}).get("language_pref", "en")
        return lang
    languages = merchant.get("identity", {}).get("languages", ["en"])
    if "hi" in languages:
        return "hi-en mix (natural Hinglish, conversational, as Indians message on WhatsApp)"
    return "English"


def _parse_response(raw: str) -> Optional[dict]:
    """Parse JSON from Claude response, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    try:
        data = json.loads(text)
        if "body" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


def _fallback_compose(merchant: dict, trigger: dict, customer: Optional[dict]) -> dict:
    """Emergency fallback when API fails — uses template-based composition."""
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
    kind = trigger.get("kind", "generic")
    offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_str = offers[0] if offers else "your current offers"

    fallback_bodies = {
        "research_digest": f"{name}, ek nayi research aaya hai aapke category mein. Kya aap details chahenge? Reply YES.",
        "perf_dip": f"{name}, aapka CTR thoda slow chal raha hai is hafte. Main ek suggestion de sakti hoon — chalega? Reply YES.",
        "perf_spike": f"{name}, aapke views is hafte ache gaye hain! Kya aap iska fayda uthana chahenge? Reply YES.",
        "recall_due": f"Hi, Dr. {name} ki taraf se — aapka 6-month checkup due hai. Ek slot book karein? Reply YES.",
        "festival_upcoming": f"{name}, festival aa raha hai — kya aap ek special offer run karna chahenge? Reply YES.",
    }

    body = fallback_bodies.get(kind, f"{name}, aapke business ke baare mein ek important update hai. Kya main share kar sakti hoon? Reply YES.")
    return {
        "body": body,
        "cta": "binary_yes_no",
        "suppression_key": trigger.get("suppression_key", f"{kind}:fallback"),
        "rationale": f"Fallback template for {kind} — API unavailable.",
    }
