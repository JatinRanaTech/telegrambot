#!/usr/bin/env python3
"""
Advanced StayResto Telegram Bot
================================
Features:
- Auto-reply & keyword detection
- Welcome message with rules
- Channel post reply + analytics
- PostgreSQL database for users and bookings
- AI chat (Google Gemini – free)
- Real booking flow (conversation)
- Admin broadcast
- Antispam rate limiting
- Multilingual support
Render‑compatible with health‑check server
"""

import os
import re
import time
import logging
import threading
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

import psycopg2
from psycopg2 import pool

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

# Google Gemini (free)
from google import genai as google_genai

# ---------------------------
# Load environment variables
# ---------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing.")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing – set it in .env")

AI_ENABLED = bool(GEMINI_API_KEY)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# Health‑check HTTP server (for Render Web Service)
# ---------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"💚 Health server listening on port {port}")
    server.serve_forever()

# ---------------------------
# PostgreSQL connection pool (thread‑safe)
# ---------------------------
db_pool = pool.ThreadedConnectionPool(2, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def return_db_connection(conn):
    db_pool.putconn(conn)

def db_execute(query, params=None, fetch=False, commit=True):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if commit:
                conn.commit()
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        return_db_connection(conn)

# ---------------------------
# Database initialisation
# ---------------------------
def init_db():
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            language TEXT DEFAULT 'en',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            type TEXT,
            title TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            check_in TEXT,
            check_out TEXT,
            guests INTEGER,
            contact TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    ]
    for q in queries:
        db_execute(q)

init_db()

# ---------------------------
# Multilingual support
# ---------------------------
LANGUAGES = {
    "en": {
        "welcome": "🎉 Welcome, {name}! We're excited to have you in StayResto.\n\n{rules}",
        "rules_group": (
            "📜 *StayResto Group Rules:*\n\n"
            "1️⃣ Be respectful to all members.\n"
            "2️⃣ No spam or self‑promotion.\n"
            "3️⃣ Discuss only StayResto bookings & travel.\n"
            "4️⃣ No sharing of personal contact details.\n"
            "5️⃣ Admins may remove violators at any time.\n\n"
            "✨ Questions? Use /help or visit [StayResto](https://stayresto.com)"
        ),
        "rules_channel": (
            "📢 *StayResto Channel Guidelines:*\n\n"
            "• All posts are official updates from StayResto.\n"
            "• Do not repost content without permission.\n"
            "• Discuss in our group: @stayrestoofficial\n\n"
            "Thank you for being part of our OTA family!"
        ),
        "booking_prompt": "📅 Ready to book? Start /book to make a reservation.",
        "help_text": (
            "🤖 *StayResto Bot Help*\n\n"
            "/start – Start\n"
            "/help – This message\n"
            "/rules – View rules\n"
            "/booking – Quick booking link\n"
            "/book – Make a booking\n"
            "/cancel – Cancel current booking\n"
            "/language <code> – Change language (e.g. /language en or /language hi)\n"
            "/broadcast – Admin broadcast\n"
            "/viewbookings – View all bookings (Admin only)"
        ),
    },
    "hi": {
        "welcome": "🎉 स्वागत है, {name}! StayResto पर आपका स्वागत है।\n\n{rules}", 
        "rules_group": (
            "📜 *StayResto समूह नियम:*\n\n"
            "1️⃣ सभी सदस्यों का सम्मान करें।\n"
            "2️⃣ स्पैम या स्व-प्रचार न करें।\n"
            "3️⃣ केवल StayResto बुकिंग और यात्रा पर चर्चा करें।\n"
            "4️⃣ व्यक्तिगत संपर्क विवरण साझा न करें।\n"
            "5️⃣ एडमिन किसी भी समय उल्लंघनकर्ताओं को हटा सकते हैं।\n\n"
            "✨ प्रश्न? /help का उपयोग करें या [StayResto](https://stayresto.com) पर जाएं"
        ),
        "rules_channel": (
            "📢 *StayResto चैनल दिशानिर्देश:*\n\n"
            "• सभी पोस्ट StayResto के आधिकारिक अपडेट हैं।\n"
            "• बिना अनुमति के सामग्री साझा न करें।\n"
            "• हमारे समूह में चर्चा करें: @stayrestoofficial\n\n"
            "हमारे OTA परिवार का हिस्सा बनने के लिए धन्यवाद!"
        ),
        "booking_prompt": "📅 बुकिंग के लिए तैयार हैं? आरक्षण करने के लिए /book का प्रयोग करें।",
        "help_text": (
            "🤖 *StayResto Bot सहायता*\n\n"
            "/start – शुरू करें\n"
            "/help – यह संदेश\n"
            "/rules – नियम देखें\n"
            "/booking – बुकिंग लिंक\n"
            "/book – बुकिंग करें\n"
            "/cancel – वर्तमान बुकिंग रद्द करें\n"
            "/language <code> – भाषा बदलें (उदा. /language en या /language hi)\n"
            "/broadcast – व्यवस्थापक प्रसारण\n"
            "/viewbookings – सभी बुकिंग देखें (केवल एडमिन)"
        ),
    },
}

def get_user_language(user_id) -> str:
    rows = db_execute("SELECT language FROM users WHERE user_id = %s", (user_id,), fetch=True)
    return rows[0][0] if rows else "en"

LANGUAGE_DISPLAY = {
    "en": "English",
    "hi": "हिन्दी",
}

def set_user_language(user_id, lang: str):
    db_execute(
        "INSERT INTO users (user_id, language) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET language = EXCLUDED.language",
        (user_id, lang)
    )

def translate(key: str, user_id: Optional[int] = None, **kwargs) -> str:
    if user_id:
        lang = get_user_language(user_id)
    else:
        lang = "en"
    strings = LANGUAGES.get(lang, LANGUAGES["en"])
    text = strings.get(key, LANGUAGES["en"].get(key, ""))
    if kwargs:
        text = text.format(**kwargs)
    return text

# ---------------------------
# Database helpers (PostgreSQL)
# ---------------------------
def save_user(user_id, username, first_name, language="en"):
    db_execute(
        "INSERT INTO users (user_id, username, first_name, language) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, "
        "first_name = EXCLUDED.first_name, language = EXCLUDED.language",
        (user_id, username, first_name, language)
    )

def save_chat(chat_id, chat_type, title):
    db_execute(
        "INSERT INTO chats (chat_id, type, title) VALUES (%s, %s, %s) "
        "ON CONFLICT (chat_id) DO NOTHING",
        (chat_id, chat_type, title)
    )

def get_all_chat_ids():
    rows = db_execute("SELECT chat_id FROM chats", fetch=True)
    return [row[0] for row in rows]

def save_booking(user_id, check_in, check_out, guests, contact):
    db_execute(
        "INSERT INTO bookings (user_id, check_in, check_out, guests, contact) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, check_in, check_out, guests, contact)
    )

# ---------------------------
# Antispam
# ---------------------------
spam_tracker: Dict[tuple, List[float]] = {}
SPAM_THRESHOLD = 5
SPAM_WINDOW = 10
MUTE_DURATION = 300

async def check_spam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    now = time.time()

    key = (chat_id, user_id)
    timestamps = [t for t in spam_tracker.get(key, []) if now - t < SPAM_WINDOW]
    timestamps.append(now)
    spam_tracker[key] = timestamps

    if len(timestamps) >= SPAM_THRESHOLD:
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(seconds=MUTE_DURATION),
            )
            username = (
                f"@{update.effective_user.username}"
                if update.effective_user.username
                else update.effective_user.first_name
            )
            await update.message.reply_text(
                f"⛔ {username} you're sending too many messages. Muted for {MUTE_DURATION}s."
            )
            logger.info(f"User {user_id} muted for spam in {chat_id}")
        except Exception as e:
            logger.error(f"Failed to mute spammer: {e}")
        return True
    return False

# ---------------------------
# AI rate limiter
# ---------------------------
ai_cooldowns: Dict[int, float] = {}
AI_COOLDOWN = 30

# ---------------------------
# AI chat
# ---------------------------
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AI_ENABLED:
        await update.message.reply_text("AI assistant is not configured.")
        return

    user_id = update.effective_user.id
    now = time.time()
    if user_id in ai_cooldowns and now - ai_cooldowns[user_id] < AI_COOLDOWN:
        remaining = int(AI_COOLDOWN - (now - ai_cooldowns[user_id]))
        await update.message.reply_text(f"⏳ Please wait {remaining}s before asking again.")
        return

    ai_cooldowns[user_id] = now

    query = update.message.text
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.effective_chat.send_action(action="typing")

    try:
        client = google_genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model="models/gemini-2.0-flash",
            contents=query,
        )
        await update.message.reply_markdown(response.text)

    except google_genai.errors.APIError as e:
        logger.error(f"Gemini API error (code={e.code}): {e.message}")
        await update.message.reply_text("AI is unavailable. Please try again later.")

    except Exception as e:
        logger.error(f"Gemini request failed: {e}")
        await update.message.reply_text("Sorry, AI temporarily unavailable.")

# ---------------------------
# Booking conversation
# ---------------------------
CHECK_IN, CHECK_OUT, GUESTS, CONTACT = range(4)

async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 *New Booking*\nPlease enter check‑in date (YYYY‑MM‑DD):", parse_mode=ParseMode.MARKDOWN)
    return CHECK_IN

async def check_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        await update.message.reply_text("Invalid format. Use YYYY-MM-DD. Try again:")
        return CHECK_IN

    try:
        check_in_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text("Invalid date. Try again:")
        return CHECK_IN

    if check_in_date < date.today():
        await update.message.reply_text("Check‑in date cannot be in the past.")
        return CHECK_IN

    context.user_data["check_in"] = date_str
    await update.message.reply_text("Check‑out date (YYYY‑MM‑DD):")
    return CHECK_OUT

async def check_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        await update.message.reply_text("Invalid format. Use YYYY-MM-DD. Try again:")
        return CHECK_OUT

    try:
        check_in_date = datetime.strptime(context.user_data["check_in"], "%Y-%m-%d").date()
        check_out_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text("Invalid date. Try again:")
        return CHECK_OUT

    if check_out_date <= check_in_date:
        await update.message.reply_text("Check‑out must be after check‑in.")
        return CHECK_OUT
    if check_out_date < date.today():
        await update.message.reply_text("Check‑out date cannot be in the past.")
        return CHECK_OUT

    context.user_data["check_out"] = date_str
    await update.message.reply_text("Number of guests:")
    return GUESTS

async def guests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        num = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Please enter a number:")
        return GUESTS
    if num < 1:
        await update.message.reply_text("At least 1 guest required.")
        return GUESTS
    context.user_data["guests"] = num
    await update.message.reply_text("Contact info (phone or email):")
    return CONTACT

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_info = update.message.text.strip()
    user_id = update.effective_user.id
    check_in = context.user_data["check_in"]
    check_out = context.user_data["check_out"]
    guests = context.user_data["guests"]
    save_booking(user_id, check_in, check_out, guests, contact_info)
    await update.message.reply_markdown(
        f"✅ Booking request received!\n"
        f"📅 {check_in} → {check_out}\n"
        f"👥 {guests} guests\n"
        f"📞 {contact_info}\n\n"
        "Our team will contact you shortly."
    )
    return ConversationHandler.END

async def book_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Booking cancelled.")
    return ConversationHandler.END

# ---------------------------
# Command handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name, language=user.language_code or "en")
    
    welcome_text = translate("welcome", user.id, name=user.mention_markdown(), rules=translate("rules_group", user.id))
    await update.message.reply_markdown(
        welcome_text,
        reply_markup=welcome_keyboard(),
        disable_web_page_preview=True
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = translate("help_text", update.effective_user.id)
    await update.message.reply_markdown(help_text)

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        rules = translate("rules_group", update.effective_user.id)
    else:
        rules = translate("rules_channel", update.effective_user.id)
    await update.message.reply_markdown(rules, disable_web_page_preview=True)

async def booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = translate("booking_prompt", update.effective_user.id)
    await update.message.reply_text(prompt)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = " ".join(context.args)
    chat_ids = get_all_chat_ids()
    success = 0
    for cid in chat_ids:
        try:
            await context.bot.send_message(cid, message)
            success += 1
        except Exception as e:
            logger.warning(f"Failed to send to {cid}: {e}")
    await update.message.reply_text(f"Broadcast sent to {success}/{len(chat_ids)} chats.")

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in LANGUAGES:
        await update.message.reply_text("Usage: /language <code>   e.g. /language hi")
        return
    lang = context.args[0]
    set_user_language(update.effective_user.id, lang)
    display_name = LANGUAGE_DISPLAY.get(lang, lang)
    await update.message.reply_text(f"✅ Language set to {display_name}.")

async def view_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized. Only admins can view bookings.")
        return

    rows = db_execute(
        "SELECT id, user_id, check_in, check_out, guests, contact, created_at "
        "FROM bookings ORDER BY created_at DESC LIMIT 20",
        fetch=True,
    )
    if not rows:
        await update.message.reply_text("📭 No bookings found.")
        return

    msg = "📋 *Last 20 bookings:*\n\n"
    for r in rows:
        msg += (
            f"*ID:* {r[0]}\n"
            f"*User:* {r[1]}\n"
            f"*Check‑in:* {r[2]}  →  *Check‑out:* {r[3]}\n"
            f"*Guests:* {r[4]}\n"
            f"*Contact:* {r[5]}\n"
            f"*Created:* {r[6].strftime('%Y-%m-%d %H:%M') if r[6] else 'N/A'}\n"
            f"──────────────────\n"
        )
    await update.message.reply_markdown(msg)

def welcome_keyboard():
    kb = [
        [InlineKeyboardButton("🌐 Visit StayResto", url="https://stayresto.com")],
        [
            InlineKeyboardButton("📘 Rules", callback_data="rules"),
            InlineKeyboardButton("📅 Book Now", callback_data="book"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "rules":
        rules = translate("rules_group", update.effective_user.id)
        await query.edit_message_text(rules, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    elif query.data == "book":
        await query.edit_message_text("Use /book to start a booking.")

async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue
        save_user(member.id, member.username, member.first_name, language=member.language_code or "en")
        welcome_msg = translate("welcome", member.id,
                        name=member.mention_markdown(),
                        rules=translate("rules_group", member.id))
        await update.message.reply_markdown(welcome_msg, disable_web_page_preview=True)

async def keyword_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    text_lower = text.lower()
    chat_type = update.effective_chat.type
    is_group = chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]

    booking_keywords = ["booking", "reservation", "hotel", "stay", "resto", "price", "mmt", "ota"]
    if any(k in text_lower for k in booking_keywords):
        await update.message.reply_markdown(
            "🏨 *StayResto at your service!*\n\n"
            "We’re a leading OTA platform offering best price guarantee.\n"
            "Use /book to start a reservation."
        )
        return

    if not AI_ENABLED:
        return

    if is_group:
        bot_username = context.bot.username.lower()
        mentioned = f"@{bot_username}" in text.lower()
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        if not mentioned and not is_reply_to_bot:
            return

    await ai_chat(update, context)

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.text:
        return
    await post.reply_markdown(
        "🚀 *Thanks for tuning in!*\n📅 Book your next stay with [StayResto](https://stayresto.com)."
    )
    if LOG_GROUP_ID:
        try:
            await context.bot.forward_message(chat_id=LOG_GROUP_ID, from_chat_id=post.chat.id, message_id=post.message_id)
        except Exception as e:
            logger.error(f"Failed to forward post to log group: {e}")

async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member.new_chat_member.status in ["member", "administrator"]:
        chat = update.effective_chat
        save_chat(chat.id, chat.type, chat.title or "")
        logger.info(f"Bot added to {chat.type}: {chat.title} ({chat.id})")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=context.error)

# ---------------------------
# Main application (with health server)
# ---------------------------
def main():
    # Start the health‑check server in a background thread BEFORE polling
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("booking", booking_command))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("language", set_language))
    app.add_handler(CommandHandler("viewbookings", view_bookings))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book", book_start)],
        states={
            CHECK_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_in)],
            CHECK_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_out)],
            GUESTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, guests)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact)],
        },
        fallbacks=[CommandHandler("cancel", book_cancel)],
    )
    app.add_handler(conv_handler)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, keyword_reply))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, channel_post_handler))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(button_handler))

    async def group_message_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        spam_detected = await check_spam(update, context)
        if spam_detected:
            raise ApplicationHandlerStop

    app.add_handler(MessageHandler(filters.TEXT & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), group_message_guard), group=1)

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            keyword_reply,
        ),
        group=2,
    )

    app.add_error_handler(error_handler)

    logger.info("🚀 Advanced StayResto Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    finally:
        if db_pool:
            db_pool.closeall()