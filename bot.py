import os
import logging
from datetime import datetime, date

import psycopg2
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

    # Insert first activity row, or update the existing monthly row.
    # A new active day is counted only when the last recorded date differs.
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

    # Ignore bot messages
    if not user or user.is_bot:
        return

    # Check user's status in the group
    try:
        member = await context.bot.get_chat_member(message.chat.id, user.id)

        # Don't count admins or group owner
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
# /stats Command
# =========================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    # Private chat
    if update.effective_chat.type == "private":
        group_id = os.getenv("GROUP_ID")
        admin_id = os.getenv("ADMIN_ID")

        if not group_id:
            await message.reply_text("⚠️ GROUP_ID সেট করা হয়নি।")
            return

        if not admin_id:
            await message.reply_text("⚠️ ADMIN_ID সেট করা হয়নি।")
            return

        # Only the configured admin can use /stats in private chat
        if str(user.id) != admin_id:
            await message.reply_text(
                "❌ এই command শুধুমাত্র group admin-দের জন্য।"
            )
            return

        target_chat_id = int(group_id)

        await message.reply_text(f"GROUP_ID = {target_chat_id}")

    # Group chat
    else:
        target_chat_id = update.effective_chat.id

        if not await is_group_admin(user.id, target_chat_id, context):
            return

    month = datetime.now().strftime("%Y-%m")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT first_name, username, message_count, active_days
                    FROM public.activity_logs
                    WHERE chat_id = %s
                      AND month = %s
                    ORDER BY message_count DESC
                    """,
                    (target_chat_id, month),
                )
                rows = cursor.fetchall()
    except Exception:
        logger.exception("Failed to fetch activity stats from PostgreSQL")
        await message.reply_text("⚠️ Database থেকে stats আনা যায়নি।")
        return

    if not rows:
        await message.reply_text(
            "📊 এই মাসে এখনো কোনো activity পাওয়া যায়নি।"
        )
        return

    text = f"📊 Activity — {month}\n\n"

    for position, row in enumerate(rows, start=1):
        first_name = row[0] or ""
        username = row[1] or ""
        message_count = row[2]
        active_days = row[3]

        if username:
            name = f"@{username}"
        else:
            name = first_name or "Unknown user"

        if position == 1:
            rank = "🥇"
        elif position == 2:
            rank = "🥈"
        elif position == 3:
            rank = "🥉"
        else:
            rank = f"{position}."

        text += (
            f"{rank} {name}\n"
            f"   💬 Messages: {message_count}\n"
            f"   📅 Active days: {active_days}\n\n"
        )

    text += (
        f"👥 Active members: {len(rows)}\n"
        f"📅 Month: {month}"
    )

    await message.reply_text(text)


# ======================
# Group Admin
# ======================

async def is_group_admin(user_id, chat_id, context):
    try:
        admins = await context.bot.get_chat_administrators(chat_id)

        for admin in admins:
            if admin.user.id == user_id:
                return True

        return False

    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return False


# ======================
# Chat ID
# ======================

async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text(
            f"🆔 Chat ID:\n{update.effective_chat.id}",
            parse_mode="Markdown",
        )


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

    app = Application.builder().token(TOKEN).build()

    # Track normal messages (not commands)
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            track_message,
        )
    )

    # /stats
    app.add_handler(CommandHandler("stats", stats))

    # /id
    app.add_handler(CommandHandler("id", chat_id))

    print("📊 Odvut Activity Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
