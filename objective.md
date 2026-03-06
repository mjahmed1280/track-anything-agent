# track-anything-agent — Project Objective

## Legacy Reference
- `../src/agent_logic.py` — original intent parser & Sheets/Firestore logic
- `../src/main.py` — original FastAPI entry point
- `.env` variables required: `GOOGLE_CLOUD_PROJECT`, `GEMINI_API_KEY`, `SPREADSHEET_ID`, `TELEGRAM_BOT_TOKEN`

---

## Objective

Refactor the existing Proof of Concept into a Model-Agnostic, Multi-Modal Agentic System.
The goal is to separate **Decision Logic** (LLM) from **Capability Execution** (Tools/MCP) and **Interface** (Telegram).

---

## 1. Proposed Repository Structure

```
/track-anything-agent
├── src/
│   ├── main.py                 # FastAPI Entry (Lifespan, Async, Telegram Webhook)
│   ├── agent/
│   │   ├── orchestrator.py     # Logic for deciding which tool to call (Model Agnostic)
│   │   ├── registry.py         # The Tool Registry (Mapping model-calls to Python functions)
│   │   └── prompts.py          # System instructions & Vision prompts
│   ├── tools/
│   │   ├── sheets_tool.py      # Google Sheets direct logic (Async)
│   │   ├── firestore_tool.py   # Firestore CRUD & Sync flags (Async)
│   │   └── vision_tool.py      # Image analysis processing
│   ├── integrations/
│   │   ├── telegram.py         # Telegram Bot API handlers (Inline Keyboards)
│   │   └── mcp_server.py       # (Optional) The MCP wrapper for the tools
│   └── utils/
│       ├── config.py           # Pydantic Settings for .env & Secrets
│       └── logger.py           # Structured logging
├── tests/                      # Local testing scripts
├── Dockerfile                  # Production-ready Cloud Run config
├── requirements.txt            # Pinned dependencies
└── .env                        # Environment variables
```

---

## 2. Core Functional Requirements

### A. Model-Agnostic Orchestration
- Move away from raw `genai.Client` calls inside logic functions.
- Implement a **Registry** where tools (`add_log`, `create_tracker`) are registered once.
- The **Orchestrator** formats these tools for Gemini (Function Calling) or Claude (Tools) via small adapter functions.

### B. Firestore-First "Async Sync"
- **Source of Truth:** All logs write to Firestore first.
- **Log Document:** Must include a `synced_to_sheets: boolean` flag.
- **Background Sync:** Non-blocking FastAPI background task pushes "unsynced" logs to Google Sheets.

### C. Multi-Modal (Vision) Flow
- Agent handles image uploads.
- Flow:
  1. Image Received
  2. Vision LLM analyzes
  3. Agent proposes: _"I found a receipt for $5.00 at Starbucks. Log to 'Expenses'?"_
  4. User clicks **Yes** via Telegram Inline Button
  5. Tool execution

### D. FastAPI Refinement
- Replace `@app.on_event("startup")` with a `lifespan` context manager.
- Fix CORS: Remove `allow_credentials=True` when `allow_origins=["*"]`.
- All Sheets/Firestore I/O must be `awaited` or run in an executor to prevent blocking the event loop.

---

## 3. Technical Constraints

| Concern | Solution |
|---------|----------|
| Agent framework | PydanticAI (tool + agent definitions) |
| Workflow / HITL | LangGraph (Human-in-the-loop checkpoint before writes) |
| LLM calls | LiteLLM (model-agnostic, defaults to `gemini/gemini-2.5-flash`) |
| Interface | `python-telegram-bot` async (`ExtBot`) |
| Config | `pydantic-settings` — no hardcoded fallbacks, strict env var loading |
| Logging | Structured logging via `utils/logger.py` |
| Typing | Pydantic models for all request/response data |

**Strict rules:**
- `load_dotenv()` at the absolute top of every entrypoint.
- `os.environ` with **no hardcoded fallbacks** for project IDs or keys.
- Every tool call returns: `{"status": "success" | "error", "message": str}` for the LLM to read.
- Telegram `/webhook` endpoint as primary interface in `main.py`.

---

## 4. Why This Architecture

**It's an Agent, not a Script:**
Instead of a linear script that logs data, this creates a "brain" (Orchestrator) that has access to "limbs" (Tools).

**Negotiation via Inline Keyboards:**
The agent doesn't act blindly — it asks for confirmation, making it feel like a personal assistant.

**Extensibility via Tool Registry:**
Adding a new integration (Spotify, Apple Health) = drop a new file in `/tools/` and register it. The Orchestrator automatically exposes it to the LLM.

---

## 5. Key Dependencies

```
fastapi
uvicorn[standard]
pydantic-settings
pydantic-ai
langgraph
litellm
python-telegram-bot[webhooks]
google-cloud-firestore
google-api-python-client
google-auth
python-dotenv
```
