# Next Steps / Backlog

## Prompt / Behaviour Fixes

### T09 Quirk — Minimum Data Requirement before add_log
- **Issue:** Agent logged "add 200" immediately to Expenses before knowing what it was for. Action-first is fast but breaks data integrity if the tracker was wrong.
- **Fix:** Add prompt rule — before calling `add_log`, require at least a Value + Label. If only a bare number is given and context is not 100% certain, ask for the Label first.
- **Risk if not fixed:** Ghost entries in wrong trackers; hard to clean up at scale.

### Table Bloat / Duplicate Rows in get_logs_summary responses
- **Issue:** T04/T08 responses showed 10 rows with many duplicates (same Groceries/DMart repeated). Cause: `conversation_history` carries previous raw tool results; LLM merges history + new tool output into one redundant table.
- **Fix:** Once a tool result has been shown to the user, summarize/collapse it in `conversation_history` rather than storing the full raw JSON. Ties into the Summarizer Node / history pruning work.

---

## Architecture / Performance

### Cold Start Latency — T01 (18.8s, no tool call)
- **Issue:** Pure LLM overhead for a simple "what trackers do we have?" query. No tool call needed.
- **Fix:** Add a fast pre-check — if intent is clearly "list trackers" and `known_trackers` is already loaded, return a static formatted response without an LLM round-trip. Drops ~18s to <1s for this class of query.

### History Pruning Node
- **Issue:** Conversation history grows unbounded; 20-message history with full table responses will hit token limits and slow latency.
- **Fix:** After each tool result is displayed, replace the raw tool output in `conversation_history` with a short summary (e.g., "Showed last 10 Expenses entries, total 2000"). Keeps history lean. `state_summary` already handles older turns but raw tool blobs in recent turns are the current bottleneck.

---

## New Tools / Features

### Duplicate Detection in add_log
- Check if the exact same (tracker, values) entry was written in the last 60 seconds before committing.
- Prevents accidental double-taps (e.g., user sending the same Telegram message twice).

### update_tracker tool
- Users will want to add columns (e.g., "add a Location column to Expenses") or rename headers (e.g., "Meal" to "Food Item").
- Needs schema migration in both Firestore config and the Sheets tab header row.

---

## Deployment (tracked in MEMORY.md)
- Cloud Run deployment + Secret Manager
- Telegram webhook registration
- Persist `state_summary` + `conversation_history` across sessions in `telegram.py` (currently stateless per message)
