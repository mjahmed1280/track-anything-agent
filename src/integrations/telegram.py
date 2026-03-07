"""
Telegram Integration — Async bot handlers using python-telegram-bot.

Supported message types:
  - Text messages  → passed to orchestrator
  - Photo messages → passed to vision_tool, then orchestrator proposes a log action
  - Callback queries (inline keyboard) → resumes LangGraph HITL checkpoint

Inline keyboard pattern:
  confirm:<thread_id>  → user approved the pending tool call
  cancel:<thread_id>   → user rejected the pending tool call
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from src.agent import orchestrator
from src.tools.firestore_tool import get_known_trackers, load_session, save_session
from src.tools.vision_tool import analyze_image
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _confirmation_keyboard(thread_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, log it", callback_data=f"confirm:{thread_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{thread_id}"),
        ]
    ])


async def _send_or_confirm(update: Update, state: dict):
    """
    If the orchestrator is waiting for HITL confirmation, send an inline keyboard.
    Otherwise, send the final response text.
    """
    chat_id = update.effective_chat.id

    if state.get("pending_tool") and state.get("confirmed") is None:
        thread_id = str(chat_id)
        tool = state["pending_tool"]
        args = state["pending_args"] or {}
        proposal = f"I want to call: *{tool}*\nWith: `{args}`\n\nShall I proceed?"
        await update.effective_message.reply_text(
            proposal,
            parse_mode="Markdown",
            reply_markup=_confirmation_keyboard(thread_id),
        )
    else:
        response_text = state.get("final_response") or "Done."
        await update.effective_message.reply_text(response_text)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm Trackbot — the bridge between your messy daily life and your structured Life OS.\n\n"
        "Send me anything to log (\"spent $12 on lunch\"), ask what you've tracked, "
        "or send a photo of a receipt and I'll handle the rest."
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    thread_id = str(update.effective_chat.id)
    logger.info(f"[Telegram] Text from {thread_id}: {user_input}")

    known_trackers = await get_known_trackers()
    session = await load_session(thread_id)
    state = await orchestrator.run(
        user_input, known_trackers, thread_id,
        conversation_history=session["conversation_history"],
        last_active_tracker=session["last_active_tracker"],
    )
    await save_session(thread_id, state)
    await _send_or_confirm(update, state)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = str(update.effective_chat.id)
    logger.info(f"[Telegram] Photo from {thread_id}")

    # Download the largest photo variant
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    await update.message.reply_text("Analyzing image...")
    vision_result = await analyze_image(bytes(image_bytes))

    if vision_result["status"] == "error":
        await update.message.reply_text(f"Could not analyze the image: {vision_result['message']}")
        return

    # Feed the vision description back into the orchestrator as a text message
    description = vision_result["description"]
    known_trackers = await get_known_trackers()
    session = await load_session(thread_id)
    state = await orchestrator.run(
        description, known_trackers, thread_id,
        conversation_history=session["conversation_history"],
        last_active_tracker=session["last_active_tracker"],
    )
    await save_session(thread_id, state)
    await _send_or_confirm(update, state)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, thread_id = query.data.split(":", 1)
    confirmed = action == "confirm"
    logger.info(f"[Telegram] HITL callback: {action} for thread {thread_id}")

    state = await orchestrator.resume(thread_id, confirmed)
    response_text = state.get("final_response") or ("Done." if confirmed else "Cancelled.")
    await query.edit_message_text(response_text)


# ── App factory ───────────────────────────────────────────────────────────────

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    return app
