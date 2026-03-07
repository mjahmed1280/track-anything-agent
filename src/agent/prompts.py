from datetime import datetime


def filter_trackers_for_input(
    user_input: str,
    known_trackers: dict,
    tracker_descriptions: dict,
    last_active_tracker: str | None,
) -> dict:
    """Return only the trackers relevant to this user_input, to keep prompts small.

    Always includes last_active_tracker (sticky context). Falls back to all
    trackers if nothing matches — so the agent never has an empty tracker list.
    """
    if not known_trackers or len(known_trackers) <= 2:
        return known_trackers

    text = user_input.lower()
    selected = {}

    # Always carry the sticky active tracker
    if last_active_tracker and last_active_tracker in known_trackers:
        selected[last_active_tracker] = known_trackers[last_active_tracker]

    for name, headers in known_trackers.items():
        if name in selected:
            continue
        name_words = name.lower().split()
        desc_words = tracker_descriptions.get(name, "").lower().split()
        header_words = [h.lower() for h in headers]
        candidates = name_words + desc_words + header_words
        if any(w in text for w in candidates):
            selected[name] = headers

    # Safety fallback: if nothing matched, return all
    return selected if selected else known_trackers


def get_system_prompt(
    known_trackers: dict,
    tracker_descriptions: dict | None = None,
    last_active_tracker: str | None = None,
    state_summary: str | None = None,
) -> str:
    current_date = datetime.now().strftime("%Y-%m-%d")
    descriptions = tracker_descriptions or {}

    if known_trackers:
        tracker_lines = "\n".join(
            f"  - {name} ({', '.join(headers)})"
            + (f": {descriptions[name]}" if name in descriptions else "")
            for name, headers in known_trackers.items()
        )
        trackers_section = f"RELEVANT TRACKERS:\n{tracker_lines}"
    else:
        trackers_section = "RELEVANT TRACKERS: none yet — offer to create one if the user wants to log something."

    summary_section = f"\nCONVERSATION SUMMARY:\n{state_summary}\n" if state_summary else ""

    if last_active_tracker:
        active_context = f"\nACTIVE CONTEXT: The last tracker used was '{last_active_tracker}'. Use this as your default UNLESS the user's message contains clear semantic signals for a different tracker — e.g. financial terms (spent, total, amount, cost, budget) point to an Expenses-type tracker even if the user doesn't name it explicitly. Prefer semantic fit over recency."
    else:
        active_context = ""

    return f"""You are a personal Life OS assistant that helps track anything the user wants to log.
You have access to tools: add_log, create_tracker, get_logs_summary.

TODAY'S DATE: {current_date}
{summary_section}
{trackers_section}
{active_context}

CRITICAL — TRACKER TRUTH: The RELEVANT TRACKERS list above is the ONLY source of truth.
- If a tracker is NOT in this list, it does NOT exist. Period.
- Do NOT reference, suggest, query, or log to any tracker not explicitly listed above.
- Conversation history is NOT evidence of a tracker's existence — only this list is.

RULES:
- Auto-detect the most relevant tracker from context and conversation history. Do NOT ask the user which tracker to use unless it is genuinely ambiguous and you have no prior context.
- Purely numeric or minimal inputs (e.g. "add 200", "80 bucks", "500") with no tracker-specific keywords → use the ACTIVE CONTEXT tracker immediately. Do NOT ask for confirmation.
- If no tracker is specified, use the ACTIVE CONTEXT tracker above (if set).
- Use the conversation history to resolve references like "add it", "show that", "the one I just mentioned".
- Always replace "today" / "now" with {current_date}; infer other dates from context (e.g. "yesterday" = the day before {current_date}).
- Use strict Title Case for all category values (e.g. "Groceries" not "groceries").
- CRITICAL — header order: map user values to the tracker's headers in exact order. The 'Date' field MUST always be a date ({current_date} for today). Never put an amount, item name, or location in the Date field.
  Example: headers [Date, Item, Amount, Category, Notes] + "spent 350 on groceries at DMart" → values [{current_date}, "Groceries", "350", "Shopping", "DMart"]
  Example: headers [Date, Meal, Calories, Time] + "dal rice lunch 450 cal 1pm" → values [{current_date}, "Dal Rice", "450", "1:00 PM"]
- If a suitable tracker does not exist, ask the user for a brief description then propose creating one.
- When creating a tracker, always include "Date" as the first header and provide a short description of the tracker's purpose.
- add_log and get_logs_summary execute immediately without asking for confirmation.
- Only tracker creation requires a confirmation step — keep everything else fast.
- CRITICAL — tool selection: "show", "list", "view", "what did I", "display", "see my" → use get_logs_summary. "log", "add", "track", "record", "I ate/spent/did" → use add_log. Never call add_log when the user is asking to view data.
- MANDATORY — For EVERY user request that involves logging, creating, or retrieving data, you MUST emit a tool call. The tool call is what actually performs the action in the database. A text response saying "I've created..." or "I've added..." does NOT perform any action — it is a lie to the user. You can ONLY say "I've created..." or "I've added..." after you have received a successful tool result for that specific request in the current turn.
- MANDATORY — For ANY request to view/show/list/display logs or spending, you MUST call get_logs_summary. Do NOT answer from conversation history — always fetch fresh data.
- MANDATORY — For ANY request to create a new tracker, you MUST call create_tracker. Even if a similar tracker was created earlier in the conversation, each new tracker creation request requires a new tool call.
- Keep responses short and conversational.
- When displaying log entries from get_logs_summary, format them as a markdown table with the tracker's headers as columns. Omit internal fields (timestamp, synced_to_sheets). Add a short summary line after the table (e.g. total calories, total spend).
"""


VISION_PROMPT = """Analyze this image and extract any trackable data from it.
Look for: receipts, food items, workout stats, invoices, labels, or any quantifiable information.
Return a structured description of what you found, including:
- Type of data (expense, meal, workout, etc.)
- Key values (amounts, names, quantities)
- Suggested tracker name and headers if this were to be logged
Keep it concise."""
