import os
import logging
from datetime import datetime, date

import psycopg2
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# =========================
# Logging
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# Environment Variables
# =========================

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")


# =========================
# Database
# =========================

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable not found!")
    return psycopg2.connect(DATABASE_URL)


def init_database():
    """Check that the expected table exists and is accessible."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'activity_logs'
            """)
            if cursor.fetchone() is None:
                raise RuntimeError(
                    "Table public.activity_logs was not found. "
                    "Create it in Supabase before starting the bot."
                )
    logger.info("PostgreSQL connection and activity_logs table verified.")


def current_month():
    return datetime.now().strftime("%Y-%m")


async def get_target_chat_id(update: Update):
    """Return the group being queried; in private, use configured GROUP_ID."""
    chat = update.effective_chat
    if not chat:
        return None

    if chat.type == "private":
        group_id = os.getenv("GROUP_ID")
        if not group_id:
            message = update.effective_message
            if message:
                await message.reply_text("⚠️ GROUP_ID সেট করা হয়নি।")
            return None
        try:
            return int(group_id)
        except ValueError:
            message = update.effective_message
            if message:
                await message.reply_text("⚠️ Render-এর GROUP_ID সঠিক numeric ID নয়।")
            return None

    return chat.id


# =========================
# Track Message
# =========================

def record_activity(message):
    chat_id = message.chat.id
    user = message.from_user

    if not user:
        return

    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    now = datetime.now()
    month = now.strftime("%Y-%m")
    today = now.date()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.activity_logs (
                    chat_id,
                    user_id,
                    username,
                    first_name,
                    month,
                    message_count,
                    active_days,
                    last_active_date
                )
                VALUES (%s, %s, %s, %s, %s, 1, 1, %s)
                ON CONFLICT (chat_id, user_id, month)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    message_count = public.activity_logs.message_count + 1,
                    active_days = public.activity_logs.active_days
                        + CASE
                            WHEN public.activity_logs.last_active_date
                                 IS DISTINCT FROM EXCLUDED.last_active_date
                            THEN 1
                            ELSE 0
                          END,
                    last_active_date = EXCLUDED.last_active_date
                """,
                (chat_id, user_id, username, first_name, month, today),
            )


# =========================
# Message Handler
# =========================

async def track_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    user = message.from_user

    if not user or user.is_bot:
        return

    try:
        member = await context.bot.get_chat_member(message.chat.id, user.id)

        # Existing behavior: do not count admins or group owner.
        if member.status in ["administrator", "creator"]:
            return

    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return

    try:
        record_activity(message)
    except Exception:
        logger.exception("Failed to record activity in PostgreSQL")


# =========================
# Formatting helpers
# =========================

def display_name(first_name, username):
    if username:
        return f"@{username}"
    return first_name or "Unknown user"


def medal(position):
    if position == 1:
        return "🥇"
    if position == 2:
        return "🥈"
    if position == 3:
        return "🥉"
    return f"{position}."


# =========================
# /stats — admin-only full summary, Top 10 display
# =========================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    target_chat_id = await get_target_chat_id(update)
    if target_chat_id is None:
        return

    if update.effective_chat.type == "private":
        admin_id = os.getenv("ADMIN_ID")
        if not admin_id:
            await message.reply_text("⚠️ ADMIN_ID সেট করা হয়নি।")
            return
        if str(user.id) != admin_id:
            await message.reply_text("❌ এই command শুধুমাত্র configured admin ব্যবহার করতে পারবেন।")
            return
    elif not await is_group_admin(user.id, target_chat_id, context):
        await message.reply_text("❌ এই command শুধুমাত্র group admin-দের জন্য।")
        return

    month = current_month()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.activity_logs
                    WHERE chat_id = %s AND month = %s
                    """,
                    (target_chat_id, month),
                )
                total_active = cursor.fetchone()[0]

                cursor.execute(
                    """
                    SELECT first_name, username, message_count, active_days
                    FROM public.activity_logs
                    WHERE chat_id = %s AND month = %s
                    ORDER BY message_count DESC, user_id ASC
                    LIMIT 10
                    """,
                    (target_chat_id, month),
                )
                rows = cursor.fetchall()
    except Exception:
        logger.exception("Failed to fetch activity stats from PostgreSQL")
        await message.reply_text("⚠️ Database থেকে stats আনা যায়নি।")
        return

    if not total_active:
        await message.reply_text("📊 এই মাসে এখনো কোনো activity পাওয়া যায়নি।")
        return

    text = f"📊 Activity — {month}\n\n"
    for position, row in enumerate(rows, start=1):
        text += (
            f"{medal(position)} {display_name(row[0], row[1])}\n"
            f"   💬 Messages: {row[2]}\n"
            f"   📅 Active days: {row[3]}\n\n"
        )

    text += (
        f"👥 Active members: {total_active}\n"
        f"🏆 Showing Top: {len(rows)}\n"
        f"📅 Month: {month}"
    )
    await message.reply_text(text)


# =========================
# /top — Top 10 members (available to members)
# =========================

async def top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message:
        return

    target_chat_id = await get_target_chat_id(update)
    if target_chat_id is None:
        return

    month = current_month()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT first_name, username, message_count, active_days
                    FROM public.activity_logs
                    WHERE chat_id = %s AND month = %s
                    ORDER BY message_count DESC, user_id ASC
                    LIMIT 10
                    """,
                    (target_chat_id, month),
                )
                rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.activity_logs
                    WHERE chat_id = %s AND month = %s
                    """,
                    (target_chat_id, month),
                )
                total_active = cursor.fetchone()[0]
    except Exception:
        logger.exception("Failed to fetch top activity")
        await message.reply_text("⚠️ Database থেকে leaderboard আনা যায়নি।")
        return

    if not rows:
        await message.reply_text("📊 এই মাসে এখনো কোনো activity পাওয়া যায়নি।")
        return

    text = f"🏆 Top 10 Activity — {month}\n\n"
    for position, row in enumerate(rows, start=1):
        text += (
            f"{medal(position)} {display_name(row[0], row[1])}\n"
            f"   💬 Messages: {row[2]}\n"
            f"   📅 Active days: {row[3]}\n\n"
        )
    text += f"👥 Active members: {total_active}\n📅 Month: {month}"
    await message.reply_text(text)


# =========================
# /mystats — current user's activity
# =========================

async def mystats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    target_chat_id = await get_target_chat_id(update)
    if target_chat_id is None:
        return

    month = current_month()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT first_name, username, message_count, active_days
                    FROM public.activity_logs
                    WHERE chat_id = %s AND user_id = %s AND month = %s
                    """,
                    (target_chat_id, user.id, month),
                )
                row = cursor.fetchone()
    except Exception:
        logger.exception("Failed to fetch personal activity")
        await message.reply_text("⚠️ Database থেকে তোমার stats আনা যায়নি।")
        return

    if not row:
        await message.reply_text("📊 এই মাসে তোমার কোনো activity record পাওয়া যায়নি।")
        return

    text = (
        f"📊 Your Activity — {month}\n\n"
        f"👤 {display_name(row[0], row[1])}\n"
        f"💬 Messages: {row[2]}\n"
        f"📅 Active days: {row[3]}"
    )
    await message.reply_text(text)


# =========================
# /rank — current user's position among tracked members
# =========================

async def rank(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    target_chat_id = await get_target_chat_id(update)
    if target_chat_id is None:
        return

    month = current_month()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT message_count, active_days
                    FROM public.activity_logs
                    WHERE chat_id = %s AND user_id = %s AND month = %s
                    """,
                    (target_chat_id, user.id, month),
                )
                own_row = cursor.fetchone()

                if own_row:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM public.activity_logs
                        WHERE chat_id = %s
                          AND month = %s
                          AND message_count > %s
                        """,
                        (target_chat_id, month, own_row[0]),
                    )
                    rank_position = cursor.fetchone()[0] + 1

                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM public.activity_logs
                        WHERE chat_id = %s AND month = %s
                        """,
                        (target_chat_id, month),
                    )
                    total_active = cursor.fetchone()[0]
    except Exception:
        logger.exception("Failed to fetch activity rank")
        await message.reply_text("⚠️ Database থেকে rank আনা যায়নি।")
        return

    if not own_row:
        await message.reply_text("📊 এই মাসে তোমার কোনো activity record নেই, তাই rank দেখানো যাচ্ছে না।")
        return

    await message.reply_text(
        f"🏅 Your Activity Rank — {month}\n\n"
        f"📍 Rank: #{rank_position} of {total_active}\n"
        f"💬 Messages: {own_row[0]}\n"
        f"📅 Active days: {own_row[1]}"
    )


# ======================
# Group Admin
# ======================

async def is_group_admin(user_id, chat_id, context):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(admin.user.id == user_id for admin in admins)
    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return False


# ======================
# Chat ID
# ======================

async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text(f"🆔 Chat ID:\n{update.effective_chat.id}")


# =========================
# Flask Health Check
# =========================

health_app = Flask(__name__)


@health_app.get("/")
def health_check():
    return "Odvut Activity Bot is running", 200


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


# =========================
# Main
# =========================

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not found!")
        return

    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable not found!")
        return

    try:
        init_database()
    except Exception:
        logger.exception("Database initialization/connection failed")
        return

    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            track_message,
        )
    )

    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("id", chat_id))

    print("📊 Odvut Activity Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
