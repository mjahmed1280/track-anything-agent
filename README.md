# Track Anything Agent

A personal Life OS assistant — a Telegram bot backed by an agentic, multi-modal AI pipeline that lets you log, track, and query anything from your daily life using natural language or photos.
![alt text](agent-flow.png)
> Send a text message or snap a photo of a receipt. The agent understands, proposes what to log, asks for your confirmation, then writes to Firestore and syncs to Google Sheets — all without touching a spreadsheet.

---

## What it does

- **Natural language logging** — "I spent $12 on lunch today" creates a structured entry in the right tracker.
- **Image understanding** — Send a photo of a receipt, invoice, or food label. A vision model extracts the data and the agent proposes the log entry.
- **Selective Human-in-the-Loop** — Only tracker *creation* pauses for an inline Telegram button ("Yes, log it" / "Cancel"). Logging and querying execute instantly.
- **Firestore-first persistence** — All writes go to Firestore first, then sync to Google Sheets asynchronously in the background.
- **Extensible tracker system** — Trackers (Expenses, Meals, Workouts, etc.) are defined dynamically. Adding a new one is a single user message away.
- **Contextual memory** — The agent remembers which tracker you last used and defaults to it on the next message, so you never have to repeat yourself.

---

## Architecture

```
Telegram (user interface)
    │
    ▼
FastAPI  (webhook receiver)
    │
    ▼
LangGraph Orchestrator  (stateful workflow graph)
    ├── parse_intent node   →  LiteLLM  →  Gemini 2.5 Flash (or any model)
    ├── [HITL checkpoint]   →  pauses graph, sends inline keyboard to user
    └── execute_tool node   →  Tool Registry  →  Firestore / Sheets / Vision
```

### Key design decisions

| Concern | Solution | Why |
|---|---|---|
| Workflow | LangGraph | Stateful, resumable graph — the HITL pause-and-resume pattern requires persistent graph state across two separate HTTP requests |
| LLM calls | LiteLLM + fallback chain | Single interface for Gemini on Vertex AI. On 429/503/400, automatically retries: `gemini-2.0-flash` → `gemini-2.5-flash` → `gemini-2.5-pro` |
| Tool dispatch | Custom Tool Registry | Decorator-based registration; adding a new capability is one file and one decorator |
| State persistence | Firestore Checkpointer | LangGraph threads survive process restarts; Firestore is already in the stack |
| Memory | Three-layer context system | Short-term history (6 turns) + rolling summary (`state_summary`) + sticky active tracker. See Memory section below. |
| Prompt engineering | Pyramid structure + dynamic injection | Trackers filtered per-request; truth assertion placed immediately after tracker list |
| Config | pydantic-settings | Strict env-var loading, no hardcoded fallbacks, IDE-friendly |
| Interface | python-telegram-bot (async) | Webhook mode; inline keyboards for HITL interaction |
| Deployment | Docker + Cloud Run | Stateless container, scales to zero |

---

## Project structure

```
track-anything-agent/
├── src/
│   ├── main.py                       # FastAPI entry point (lifespan, webhook endpoint)
│   ├── agent/
│   │   ├── orchestrator.py           # LangGraph graph definition + run/resume functions
│   │   ├── registry.py               # Tool registry (decorator-based, LiteLLM schema export)
│   │   └── prompts.py                # System prompt + vision prompt
│   ├── tools/
│   │   ├── sheets_tool.py            # Google Sheets read/write (async)
│   │   ├── firestore_tool.py         # Firestore CRUD + sync flag management
│   │   └── vision_tool.py            # Image analysis via Gemini Vision
│   ├── integrations/
│   │   ├── telegram.py               # Bot handlers: text, photo, inline callback
│   │   └── mcp_server.py             # MCP wrapper exposing tools to external clients
│   └── utils/
│       ├── config.py                 # pydantic-settings env config
│       ├── logger.py                 # Structured logging
│       └── firestore_checkpointer.py # LangGraph checkpointer backed by Firestore
├── tests/
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## How memory and context work

The agent manages context across turns using three cooperating layers:

**Layer 1 — Recent conversation history**
The last 6 message pairs (user + assistant) are passed to the LLM on every turn. This window is intentionally small — old turns flow into the summary layer rather than bloating the context window indefinitely. The history lets the LLM resolve references like "add it", "show that", "the one I just mentioned".

**Layer 2 — Rolling conversation summary**
When `conversation_history` exceeds 10 messages, the agent automatically condenses the older portion into a `state_summary` string via a lightweight LLM call. This summary is injected into the system prompt as a `CONVERSATION SUMMARY` block. The result: context from earlier in the session is retained without ever growing the raw message list beyond 6 pairs.

**Layer 3 — Sticky active tracker**
`AgentState` carries a `last_active_tracker` field. After every tool call that targets a tracker, this field is updated and injected into the system prompt as:

```
ACTIVE CONTEXT: The last tracker used was 'Expenses'. If the user's message
doesn't explicitly name a different tracker, default to 'Expenses'.
```

This means you can say "add 200" right after logging an expense and the agent correctly routes it to Expenses — without asking.

**Dynamic tracker injection**
Rather than dumping every tracker into every prompt, `filter_trackers_for_input()` matches the user's message against tracker names, header names, and description keywords. Only relevant trackers are shown. With many trackers this cuts prompt size by 50–80%. The active tracker is always included as a safety anchor, and the fallback is to show all trackers if nothing matches.

**Context blindness correction**
If the LLM previously said "no trackers exist" (e.g. at session start) but trackers have since been created, a `[System Correction]` message is injected just before the current user turn to override the stale belief. This prevents the agent from hallucinating an empty state.

**Prompt pyramid structure**
The system prompt is ordered so the most authoritative information comes first:
1. Core identity + today's date
2. Conversation summary (if any)
3. Relevant trackers (filtered, ground-truth labelled)
4. Active context (sticky tracker)
5. Critical truth assertion — immediately after the tracker list
6. Rules

**Context switching** is handled by the LLM using semantic understanding. "How much have I spent total?" after a Food Intake log correctly switches back to Expenses because the question is semantically about spending, not food — and both trackers are visible in the prompt when only 2 exist.

---

## User flow

**Logging (no confirmation needed)**
```
User: "spent $4.50 on coffee"
    │
    ▼
LLM infers tracker (Expenses) → calls add_log with correct field mapping
    │
    ▼
Firestore write → background sync to Google Sheets
    │
    ▼
Bot: "Logged! Coffee - $4.50 added to Expenses."
```

**Creating a new tracker (confirmation required)**
```
User: "create an Expenses tracker with columns Item, Amount, Category, Notes"
    │
    ▼
LLM calls create_tracker → graph pauses at HITL checkpoint
    │
    ▼
Bot sends inline keyboard: "Create tracker 'Expenses'? [Yes] [Cancel]"
    │
    ▼  (user clicks Yes)
Graph resumes → Firestore write
    │
    ▼
Bot: "Done! Expenses tracker is ready."
```

For **photo messages**, the vision model runs first and its structured description is fed into the same orchestrator flow.

---

## Test results

The agent ships with a 10-scenario automated test suite (`tests/test_agent.py`) that runs end-to-end against live Firestore — no mocks.

**Latest run: 9/10 pass**

| Test | What it checks | Status |
|---|---|---|
| T01 | Query when database is empty | PASS |
| T02 | Create tracker with HITL confirm | PASS |
| T03 | Log expense — auto-detect tracker, correct field order | PASS |
| T04 | Show logs without naming tracker (memory: sticky Expenses) | PASS |
| T05 | Log with natural language date ("this morning") | PASS |
| T06 | Create second tracker with HITL confirm | PASS |
| T07 | Log to new tracker — auto-detect from message content | PASS |
| T08 | Context switch: spending query after food log → back to Expenses | PASS |
| T09 | Ambiguous input ("add 200") — agent asks for clarification | PASS |
| T10 | Decline tracker creation (cancel HITL) | PASS |

To run a clean test:
```bash
python tests/clear_data.py   # wipe Firestore + Sheets
python tests/test_agent.py   # run all 10 scenarios
```

---

## Tech stack

- **Python 3.12**
- **FastAPI** + **uvicorn** — async webhook server
- **LangGraph** — stateful agentic workflow with HITL checkpoint
- **LiteLLM** — model-agnostic LLM interface with Vertex AI fallback chain: `gemini-2.0-flash` → `gemini-2.5-flash` → `gemini-2.5-pro`
- **python-telegram-bot** — async Telegram bot with inline keyboards
- **Google Cloud Firestore** — primary data store and LangGraph checkpointer
- **Google Sheets API** — secondary sync target
- **pydantic-settings** — environment configuration
- **Docker** — containerised for Cloud Run deployment

---

## Known issues and roadmap

### Current gaps

| Issue | Detail | Workaround / Fix |
|---|---|---|
| `telegram.py` is stateless | Each Telegram message starts a fresh `conversation_history` and `last_active_tracker`. Session context is lost between messages. | Persist `conversation_history`, `last_active_tracker`, and `state_summary` in Firestore keyed by `chat_id`. |
| Filter threshold brittle at exactly 2 trackers | With 2 trackers, filtering is skipped entirely (both always shown). Keyword matching is too literal for semantic synonyms like "spent" → Expenses. | Threshold already raised to `<= 2`; longer-term, use stemming or embed a small synonym table for common domains. |
| T01 slow (18s+) | On an empty database the LLM still gets a full tool-equipped prompt. No tracker exists so the response is direct, but the round-trip is slow. | Route "list trackers" queries through a lightweight check before hitting the LLM. |
| No session persistence on restart | `state_summary` and `conversation_history` are in-memory in the AgentState; a process restart resets them. | Write these fields to Firestore after each turn alongside the LangGraph checkpoint. |
| Telegram handler does not carry `state_summary` forward | The `run()` signature accepts `state_summary` but `telegram.py` does not pass it. | Thread-level state dict in `telegram.py`, same pattern as `conversation_history`. |

### Roadmap

- [ ] Cloud Run deployment + Secret Manager for credentials
- [ ] Telegram webhook registration script
- [ ] Per-user session state in Firestore (history + summary persistence across restarts)
- [ ] Background sync health-check endpoint
- [ ] Richer tracker filtering: synonym expansion for spending/food/sleep domains
- [ ] Multi-user support (user isolation in Firestore)

---

## Environment variables

```bash
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
VERTEX_LOCATION=us-central1                          # optional, default: us-central1
GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json # Vertex AI + Firestore service account
SPREADSHEET_ID=your-google-sheets-id
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
```

The LLM is called via **Vertex AI** (not the Gemini API directly). `GEMINI_API_KEY` is not used. The service account needs the `Vertex AI User` and `Cloud Datastore User` roles.

Copy `.env.example` to `.env` and fill in the values.

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Webhook mode (requires a public URL, e.g. via ngrok)
python src/main.py

# Polling mode (no public URL needed — good for local dev)
python run_polling.py
```

---

## Deployment

```bash
docker build -t track-anything-agent .
docker run --env-file .env -p 8080:8080 track-anything-agent
```

For Cloud Run, set the environment variables as secrets and point Telegram's webhook to the deployed service URL.
