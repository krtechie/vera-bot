# Vera AI Bot — magicpin AI Challenge Submission

A production-grade Merchant AI assistant that composes highly specific, grounded, context-aware WhatsApp messages for Indian merchants using Claude AI.

---

## Approach

### Core Architecture

```
Judge Harness → /v1/context → ContextStore (versioned, idempotent)
                → /v1/tick  → TriggerRouter → Composer → Claude API → Action
                → /v1/reply → ConversationManager → Composer → Action
```

### The 4-Layer Composition Strategy

Every message is composed using all four context layers:

1. **Category** — voice, tone, taboos, peer benchmarks, seasonal beats, trend signals
2. **Merchant** — CTR vs peer, active offers, review themes, lapsed customers, signals
3. **Trigger** — the ONE signal driving this message (research digest, perf spike, recall, etc.)
4. **Customer** (optional) — language, last visit, services, slot preferences

### Trigger-Kind Routing

Each trigger `kind` maps to a specialized prompt strategy with different compulsion levers:

| Trigger Kind | Primary Lever | CTA Type |
|---|---|---|
| `research_digest` | Curiosity + Reciprocity | open_ended |
| `regulation_change` | Urgency + Authority | binary_yes_no |
| `perf_spike` | Curiosity + Opportunity | open_ended |
| `perf_dip` | Loss aversion | binary_yes_no |
| `recall_due` | Specificity + Relationship | multi-choice slot |
| `festival_upcoming` | Urgency + Local context | binary_yes_no |
| `curious_ask_due` | Asking the merchant | open_ended |
| `competitor_opened` | Loss aversion + Action | binary_yes_no |
| `milestone_reached` | Reciprocity + Momentum | open_ended |
| `dormant_with_vera` | Light curiosity | open_ended |

### What Makes This Score High

**Specificity**: Every message anchors on verifiable facts from the context — actual CTR numbers, peer benchmarks, offer prices, patient counts, research citation page numbers.

**No hallucination**: System prompt strictly instructs Claude to use only facts present in the pushed context. If a digest item isn't found, no citation is made.

**Language matching**: When merchant's `identity.languages` includes `"hi"`, messages are composed in natural Hinglish (hi-en code-mix) as Indians actually message on WhatsApp.

**Compulsion levers used per trigger type** — not generic. Research triggers use curiosity + reciprocity. Perf dips use loss aversion. Curious-ask triggers ask the merchant a question.

### Multi-Turn Intelligence

- **Auto-reply detection**: Pattern-matches 14 known canned phrases. On 1st → send one nudge. On 2nd → wait 24h. On 3rd → end conversation.
- **Intent transition**: Detects "let's do it / kar do / go ahead" → immediately switches from qualifying to action mode (concrete deliverable + binary CONFIRM CTA).
- **Hard opt-out**: Detects "stop messaging / not interested / band karo" → sends polite exit + suppresses merchant for 30 days.
- **Out-of-scope redirect**: Detects off-topic asks (GST, loans, hotels) → declines politely + redirects to original thread.
- **Anti-repetition**: Fingerprints prior messages in each conversation. Won't send the same body twice.
- **Turn depth guard**: Gracefully closes conversations after 5 turns.

### Suppression Logic

- Per-trigger suppression: Once a trigger fires, it's suppressed for 7 days.
- Per-merchant suppression: After hard opt-out, merchant suppressed for 30 days.
- Conversation dedup: One open conversation per `(merchant_id, trigger_id)` pair.

---

## Tradeoffs Made

1. **Single-worker deployment**: To keep context in memory across requests, using one Uvicorn worker. This works for the test window. For production scale, would use Redis for context storage and multiple workers.

2. **Temperature = 0**: Fully deterministic for the same inputs. Required by the spec. The prompt engineering does the heavy lifting to produce quality output.

3. **Fallback composition**: If the Claude API fails, a template-based fallback fires. It's lower quality but keeps the bot online. In the test window this should never be needed.

4. **Context I wish I had**: Real merchant slot availability (so customer-facing recall messages could show actual open times rather than inferred ones), and live Google Business Profile completeness scores.

---

## Deployment

### Option 1: Render (recommended — free tier)

1. Push this repo to GitHub
2. Connect to [render.com](https://render.com) → New Web Service → connect repo
3. Add env var: `ANTHROPIC_API_KEY=sk-ant-...`
4. Deploy → get your public URL

### Option 2: Railway

```bash
railway login
railway new
railway add --service
railway env set ANTHROPIC_API_KEY=sk-ant-...
railway up
```

### Option 3: Local + ngrok

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --host 0.0.0.0 --port 8080
# In another terminal:
ngrok http 8080
```

---

## Local Testing

```bash
# Health check
curl http://localhost:8080/v1/healthz

# Push a category
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d @dataset/categories/dentists.json

# Trigger a tick
curl -X POST http://localhost:8080/v1/tick \
  -H "Content-Type: application/json" \
  -d '{"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}'
```

Run the judge simulator:
```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

---

## File Structure

```
vera-bot/
├── main.py           # FastAPI app — all 5 endpoints
├── composer.py       # Claude-powered message composition with trigger routing
├── context_store.py  # Versioned in-memory context storage
├── conversation.py   # Multi-turn conversation management, suppression, detection
├── requirements.txt
├── Dockerfile
├── render.yaml       # Render.com deployment config
├── Procfile          # Railway/Heroku deployment
└── README.md
```
>>>>>>> 58a2c9a (vera-bot: magicpin AI challenge submission)
