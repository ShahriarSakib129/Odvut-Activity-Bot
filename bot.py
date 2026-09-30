import os
import logging
import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

# =========================
# Configuration
# =========================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

DHAKA = ZoneInfo("Asia/Dhaka")
MIN_ACTIVE_DAYS = 5
ACTIVE_DAYS_CAP = 20
ACTIVE_HOURS_CAP = 20
MESSAGE_COUNT_CAP = 500
WEIGHT_DAYS = 0.40
WEIGHT_TIME = 0.35
WEIGHT_MESSAGES = 0.25

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

health_app = Flask(__name__)


@health_app.get("/")
def health_check():
    return "Odvut Activity Bot is running", 200


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable not found!")
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def init_database():
    """Verify existing table and add new activity-time columns safely."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'activity_logs'
            """)
            if cursor.fetchone() is None:
                raise RuntimeError(
                    "Table public.activity_logs was not found. "
                    "Create it in Supabase before starting the bot."
                )
            cursor.execute("""
                ALTER TABLE public.activity_logs
                ADD COLUMN IF NOT EXISTS activity_time_seconds BIGINT NOT NULL DEFAULT 0
            """)
            cursor.execute("""
                ALTER TABLE public.activity_logs
                ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS public.activity_logs_backup (
                    backup_id BIGSERIAL PRIMARY KEY,
                    backup_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    month TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    active_days INTEGER NOT NULL DEFAULT 0,
                    last_active_date DATE,
                    activity_time_seconds BIGINT NOT NULL DEFAULT 0,
                    last_message_at TIMESTAMPTZ
                )
            """)
        conn.commit()
    logger.info("Database verified; activity-time columns are ready.")


def configured_group_id():
    if not GROUP_ID_RAW:
        raise RuntimeError("GROUP_ID environment variable not found!")
    return int(GROUP_ID_RAW)


def current_month_and_date():
    now_local = datetime.now(DHAKA)
    return now_local.strftime("%Y-%m"), now_local.date()


def requested_month(context, command_name):
    """Validate optional YYYY-MM argument; default to current Dhaka month."""
    if len(context.args) > 1:
        raise ValueError(f"ব্যবহার: /{command_name} অথবা /{command_name} YYYY-MM")
    if not context.args:
        return current_month_and_date()[0]
    value = context.args[0].strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"ভুল মাসের ফরম্যাট। ব্যবহার করুন: /{command_name} YYYY-MM (যেমন /{command_name} 2026-09)") from exc
    if parsed.strftime("%Y-%m") != value:
        raise ValueError(f"ভুল মাসের ফরম্যাট। ব্যবহার করুন: /{command_name} YYYY-MM")
    return value


def display_name(first_name, username, user_id=None):
    if username:
        return f"@{username}"
    return first_name or (f"User {user_id}" if user_id else "Unknown user")


def medal(position):
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"{position}.")


def duration_text(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def normalized_score(value, cap):
    if cap <= 0:
        return 0.0
    return min(max(float(value or 0), 0.0) / cap, 1.0) * 100.0


def calculate_score(row):
    days_part = normalized_score(row["active_days"], ACTIVE_DAYS_CAP) * WEIGHT_DAYS
    hours = (row.get("activity_time_seconds") or 0) / 3600.0
    time_part = normalized_score(hours, ACTIVE_HOURS_CAP) * WEIGHT_TIME
    messages_part = normalized_score(row["message_count"], MESSAGE_COUNT_CAP) * WEIGHT_MESSAGES
    return round(days_part + time_part + messages_part, 2)


def get_month_rows(chat_id, month):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""
                SELECT chat_id, user_id, username, first_name, month,
                       message_count, active_days, last_active_date,
                       activity_time_seconds, last_message_at
                FROM public.activity_logs
                WHERE chat_id = %s AND month = %s
            """, (chat_id, month))
            rows = cursor.fetchall()
    for row in rows:
        row["score"] = calculate_score(row)
    return rows


def sort_rows(rows):
    return sorted(
        rows,
        key=lambda row: (
            -row["score"],
            -int(row["active_days"] or 0),
            -int(row["activity_time_seconds"] or 0),
            -int(row["message_count"] or 0),
            int(row["user_id"]),
        ),
    )


def eligible_rows(rows):
    return sort_rows([
        row for row in rows
        if int(row["active_days"] or 0) >= MIN_ACTIVE_DAYS
    ])


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return False

    # Configured ADMIN_ID may use admin commands in private or group chat.
    if ADMIN_ID_RAW and str(user.id) == ADMIN_ID_RAW:
        return True

    # Other Telegram group admins may use admin commands only in configured group.
    try:
        target_group = configured_group_id()
    except Exception:
        return False

    if chat.id != target_group:
        return False

    try:
        member = await context.bot.get_chat_member(target_group, user.id)
        return member.status in ("administrator", "creator")
    except Exception as exc:
        logger.warning("Admin check failed: %s", exc)
        return False


async def reject_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text("❌ এই command শুধু admin-দের জন্য।")


# =========================
# Track member messages
# =========================
def record_activity(message):
    group_id = configured_group_id()
    chat_id = message.chat.id
    user = message.from_user
    if chat_id != group_id or not user:
        return

    month, today = current_month_and_date()
    now_utc = datetime.now(timezone.utc)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO public.activity_logs (
                    chat_id, user_id, username, first_name, month,
                    message_count, active_days, last_active_date,
                    activity_time_seconds, last_message_at
                )
                VALUES (%s, %s, %s, %s, %s, 1, 1, %s, 0, %s)
                ON CONFLICT (chat_id, user_id, month)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    message_count = public.activity_logs.message_count + 1,
                    active_days = public.activity_logs.active_days +
                        CASE
                            WHEN public.activity_logs.last_active_date
                                 IS DISTINCT FROM EXCLUDED.last_active_date
                            THEN 1 ELSE 0
                        END,
                    last_active_date = EXCLUDED.last_active_date,
                    activity_time_seconds =
                        public.activity_logs.activity_time_seconds +
                        CASE
                            WHEN public.activity_logs.last_message_at IS NOT NULL
                             AND EXCLUDED.last_message_at >= public.activity_logs.last_message_at
                             AND EXCLUDED.last_message_at - public.activity_logs.last_message_at
                                 <= INTERVAL '5 minutes'
                            THEN GREATEST(
                                0,
                                FLOOR(EXTRACT(EPOCH FROM
                                    (EXCLUDED.last_message_at - public.activity_logs.last_message_at)
                                ))::BIGINT
                            )
                            ELSE 0
                        END,
                    last_message_at = EXCLUDED.last_message_at
            """, (
                chat_id, user.id, user.username or "", user.first_name or "",
                month, today, now_utc
            ))


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.is_bot:
        return

    try:
        group_id = configured_group_id()
    except Exception:
        logger.exception("GROUP_ID configuration error")
        return

    if message.chat.id != group_id:
        return

    try:
        member = await context.bot.get_chat_member(group_id, user.id)
        # Keep existing behavior: admins and creator are excluded from member stats.
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        logger.exception("Could not check member status")
        return

    try:
        record_activity(message)
    except Exception:
        logger.exception("Failed to record activity in PostgreSQL")


# =========================
# /myrank — member's own stats/rank
# =========================

        # Admin /myrank replies — sequential order
ADMIN_REPLIES = [
    "👑 আপনি Admin! আপনার আবার Activity Score কীসের? আপনি তো activity-র হিসাব রাখেন! 😎",
    "😂 আপনি হিসাব রাখেন সবার, আপনার হিসাব রাখবে কে?",
    "🫡 Admin সাহেব, নিজের rank নিয়ে এত চিন্তা কেন? Group সামলান!",
    "🏆 আপনার Rank: Admin Supreme! এই leaderboard-এ সেই rank-এর জায়গা নেই।",
    "📢 আপনি Admin, আপনার activity গোপনীয়… অন্তত এই বটের কাছে! 🤫",
    "🤣 আপনি /myrank দিয়েছেন কেন? নিজের কাছে নিজের রিপোর্ট জমা দেবেন নাকি?",
    "👀 Admin হয়েও নিজের activity দেখতে চান? সন্দেহজনক ব্যাপার!",
    "☕ আগে চা খান Admin সাহেব, Activity Score দিয়ে কী করবেন?",
    "🫵 আপনি তো নিয়ম বানান! নিজের জন্য আবার নিয়মের দরকার কী?",
    "🚨 সতর্কবার্তা: Admin-এর অতিরিক্ত rank-checking শনাক্ত করা হয়েছে!",
    "🤖 আমার database-এ আপনার rank নেই, কারণ আপনাকে হিসাবের বাইরে রাখা হয়েছে!",
    "😎 আপনি leaderboard দেখেন, leaderboard আপনাকে দেখে না!",
    "📊 আপনার Activity Report: Admin হওয়াটাই আপনার সবচেয়ে বড় activity!",
    "😂 আপনি কি নিজেকেও group থেকে ban করে activity বাড়াতে চান?",
    "👑 Admin-এর rank জানতে হলে আগে Bot-এর permission নিতে হবে!",
    "🫡 আপনার কাজ member-দের active রাখা, নিজের score দেখে active হওয়া নয়!",
    "🤔 আপনি Admin, নাকি নিজের fan club-এর president?",
    "📢 এই command সাধারণ সদস্যদের জন্য। Admin-দের জন্য আছে শুধু দায়িত্ব আর দুশ্চিন্তা!",
    "💀 আবার /myrank? Admin সাহেব, আপনার কি leaderboard-এর সঙ্গে personal শত্রুতা আছে?",
    "🎖️ অভিনন্দন! আপনি আজও Admin পদে বহাল আছেন। এর চেয়ে বড় achievement আর কী!"
]

admin_reply_index = 0
# =========================
# /myrank — member's own stats/rank
# =========================
async def myrank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_reply_index

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    if await require_admin(update, context):
        reply = ADMIN_REPLIES[admin_reply_index]
        admin_reply_index = (admin_reply_index + 1) % len(ADMIN_REPLIES)

        await message.reply_text(reply)
        return

    # নিচে non-admin সদস্যদের আগের /myrank code থাকবে

    try:
        group_id = configured_group_id()
        month, _ = current_month_and_date()
        rows = get_month_rows(group_id, month)
    except Exception:
        logger.exception("Could not fetch /myrank data")
        await message.reply_text("⚠️ Database থেকে আপনার rank আনা যায়নি।")
        return

    own = next((row for row in rows if int(row["user_id"]) == user.id), None)
    if not own:
        await message.reply_text("📊 এই মাসে আপনার কোনো activity record নেই।")
        return

    ranked = eligible_rows(rows)
    rank_position = next(
        (index for index, row in enumerate(ranked, start=1)
         if int(row["user_id"]) == user.id),
        None,
    )
    rank_text = f"#{rank_position}" if rank_position else "Not eligible"
    reply = (
        f"📊 আপনার মাসিক Activity — {month}\n\n"
        f"🏅 Rank: {rank_text}\n"
        f"⭐ Activity Score: {own['score']:.2f}/100\n"
        f"💬 Messages: {own['message_count']}\n"
        f"📅 Active Days: {own['active_days']}\n"
        f"⏱ Estimated Active Time: {duration_text(own['activity_time_seconds'])}"
    )
    if rank_position is None:
        reply += (
            f"\n\nLeaderboard rank পেতে মাসে অন্তত "
            f"{MIN_ACTIVE_DAYS} দিন active হতে হবে।"
        )
    await message.reply_text(reply)


# =========================
# /top — admin-only Top 10
# =========================
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return

    try:
        group_id = configured_group_id()
        month = requested_month(context, "top")
        all_rows = get_month_rows(group_id, month)
        rows = eligible_rows(all_rows)[:10]
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Could not fetch leaderboard")
        await message.reply_text("⚠️ Database থেকে leaderboard আনা যায়নি।")
        return

    if not rows:
        await message.reply_text(
            f"এই মাসে এখনো {MIN_ACTIVE_DAYS} active days পূরণ করা eligible member নেই।"
        )
        return

    lines = [
        f"🏆 Monthly Activity Leaderboard — {month}",
        "Ranking: Activity Score / 100",
        "",
    ]
    for position, row in enumerate(rows, start=1):
        lines.append(
            f"{medal(position)} {display_name(row['first_name'], row['username'], row['user_id'])}"
            f" — {row['score']:.2f}/100\n"
            f"   📅 {row['active_days']} days | "
            f"⏱ {duration_text(row['activity_time_seconds'])} | "
            f"💬 {row['message_count']} messages"
        )
    await message.reply_text("\n".join(lines))


# =========================
# /stats — admin-only full member stats (eligible + not eligible)
# =========================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return

    try:
        group_id = configured_group_id()
        month = requested_month(context, "stats")
        all_rows = sort_rows(get_month_rows(group_id, month))
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Could not fetch /stats")
        await message.reply_text("⚠️ Database থেকে stats আনা যায়নি।")
        return

    if not all_rows:
        await message.reply_text(f"📊 {month} মাসে এখনো কোনো activity পাওয়া যায়নি।")
        return

    eligible_count = sum(
        1 for row in all_rows
        if int(row["active_days"] or 0) >= MIN_ACTIVE_DAYS
    )
    header = [
        f"📊 Group Activity Summary — {month}",
        f"👥 Tracked members: {len(all_rows)}",
        f"🏅 Eligible members (≥{MIN_ACTIVE_DAYS} active days): {eligible_count}",
        f"📋 নিচে eligible ও not eligible—সবার stats দেওয়া হলো (Activity Score অনুযায়ী সাজানো):",
        "",
    ]
    chunks = []
    current = "\n".join(header)
    for position, row in enumerate(all_rows, start=1):
        is_eligible = int(row["active_days"] or 0) >= MIN_ACTIVE_DAYS
        status = "Eligible" if is_eligible else "Not eligible"
        member_line = (
            f"{position}. {display_name(row['first_name'], row['username'], row['user_id'])}\n"
            f"   Score: {row['score']:.2f}/100 | Status: {status}\n"
            f"   Active days: {row['active_days']} | Messages: {row['message_count']}\n"
            f"   Estimated active time: {duration_text(row['activity_time_seconds'])}"
        )
        if len(current) + len(member_line) + 2 > 3500:
            chunks.append(current)
            current = f"📊 {month} — continued\n\n" + member_line
        else:
            current += "\n\n" + member_line
    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.reply_text(chunk)


# =========================
# /stat @username — admin-only individual stats
# =========================
async def stat_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return

    if not context.args:
        await message.reply_text("ব্যবহার: /stat @username")
        return

    wanted = context.args[0].lstrip("@").strip().casefold()
    if not wanted:
        await message.reply_text("ব্যবহার: /stat @username")
        return

    try:
        group_id = configured_group_id()
        month, _ = current_month_and_date()
        rows = get_month_rows(group_id, month)
    except Exception:
        logger.exception("Could not fetch member stats")
        await message.reply_text("⚠️ Database থেকে member stats আনা যায়নি।")
        return

    target = next(
        (row for row in rows if (row.get("username") or "").casefold() == wanted),
        None,
    )
    if not target:
        await message.reply_text(
            "এই মাসে username-টি পাওয়া যায়নি। সদস্যকে group-এ message দিতে হবে "
            "এবং Telegram username সেট থাকতে হবে।"
        )
        return

    ranked = eligible_rows(rows)
    rank_position = next(
        (index for index, row in enumerate(ranked, start=1)
         if int(row["user_id"]) == int(target["user_id"])),
        None,
    )
    await message.reply_text(
        f"📊 Member Stats — {display_name(target['first_name'], target['username'], target['user_id'])}\n\n"
        f"🏅 Rank: {f'#{rank_position}' if rank_position else 'Not eligible'}\n"
        f"⭐ Activity Score: {target['score']:.2f}/100\n"
        f"💬 Messages: {target['message_count']}\n"
        f"📅 Active Days: {target['active_days']}\n"
        f"⏱ Estimated Active Time: {duration_text(target['activity_time_seconds'])}"
    )


# =========================
# Admin Data Management: export, backup, reset with confirmation
# =========================
BACKUP_COLUMNS = (
    "chat_id, user_id, username, first_name, month, message_count, "
    "active_days, last_active_date, activity_time_seconds, last_message_at"
)


def backup_rows(cursor, group_id, month=None):
    """Copy rows into durable backup table before destructive operations."""
    sql = f"""
        INSERT INTO public.activity_logs_backup (
            chat_id, user_id, username, first_name, month, message_count,
            active_days, last_active_date, activity_time_seconds, last_message_at
        )
        SELECT chat_id, user_id, username, first_name, month, message_count,
               active_days, last_active_date, activity_time_seconds, last_message_at
        FROM public.activity_logs WHERE chat_id = %s
    """
    params = [group_id]
    if month:
        sql += " AND month = %s"
        params.append(month)
    cursor.execute(sql, tuple(params))
    return cursor.rowcount


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return
    try:
        month = requested_month(context, "export")
        group_id = configured_group_id()
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT {BACKUP_COLUMNS} FROM public.activity_logs WHERE chat_id=%s AND month=%s ORDER BY user_id", (group_id, month))
                rows = cur.fetchall()
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("CSV export failed")
        await message.reply_text("⚠️ CSV export করা যায়নি।")
        return

    if not rows:
        await message.reply_text(f"{month} মাসের কোনো activity data পাওয়া যায়নি।")
        return
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in row.items()})
    payload = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    payload.name = f"activity_{month}.csv"
    await message.reply_document(document=InputFile(payload, filename=payload.name), caption=f"📁 Activity CSV — {month}\nMembers: {len(rows)}")


async def backup_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return
    try:
        if len(context.args) > 1:
            raise ValueError("ব্যবহার: /backup অথবা /backup YYYY-MM")
        month = requested_month(context, "backup") if context.args else None
        group_id = configured_group_id()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                count = backup_rows(cur, group_id, month)
        scope = month or "সব মাস"
        await message.reply_text(f"✅ Backup সম্পন্ন।\nScope: {scope}\nBackup rows: {count}\nমূল activity data অপরিবর্তিত আছে।")
    except ValueError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("Backup failed")
        await message.reply_text("⚠️ Backup সম্পন্ন করা যায়নি।")


async def reset_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return
    try:
        if len(context.args) != 1:
            raise ValueError("ব্যবহার: /resetstats YYYY-MM\nউদাহরণ: /resetstats 2026-09")
        month = requested_month(context, "resetstats")
        group_id = configured_group_id()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.activity_logs WHERE chat_id=%s AND month=%s", (group_id, month))
                count = cur.fetchone()[0]
        if count == 0:
            await message.reply_text(f"{month} মাসে reset করার মতো কোনো record নেই।")
            return
        context.user_data["pending_reset_month"] = month
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Backup করে Reset", callback_data=f"reset_confirm:{month}"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]])
        await message.reply_text(
            f"⚠️ নিশ্চিত করুন: {month} মাসের {count}টি activity record মুছে ফেলা হবে।\n"
            "Reset করার আগে স্বয়ংক্রিয় backup তৈরি হবে। Backup সফল না হলে reset হবে না।",
            reply_markup=keyboard,
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("Reset pre-check failed")
        await message.reply_text("⚠️ Reset যাচাই করা যায়নি; কোনো data মুছিনি।")


async def reset_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not await require_admin(update, context):
        await query.edit_message_text("❌ এই action শুধু admin-দের জন্য।")
        return
    if query.data == "reset_cancel":
        context.user_data.pop("pending_reset_month", None)
        await query.edit_message_text("Reset বাতিল করা হয়েছে। কোনো data পরিবর্তন হয়নি।")
        return

    month = context.user_data.get("pending_reset_month")
    expected = f"reset_confirm:{month}" if month else None
    if not month or query.data != expected:
        await query.edit_message_text("⚠️ Confirmation expired/invalid. নতুন করে /resetstats YYYY-MM দিন।")
        return
    try:
        group_id = configured_group_id()
        # Same DB transaction: backup first, verify backup count, then delete.
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.activity_logs WHERE chat_id=%s AND month=%s", (group_id, month))
                before_count = cur.fetchone()[0]
                if before_count == 0:
                    await query.edit_message_text("এই মাসে আর কোনো record নেই; reset করা হয়নি।")
                    return
                backed_up = backup_rows(cur, group_id, month)
                if backed_up != before_count:
                    raise RuntimeError(f"Backup verification mismatch: expected {before_count}, copied {backed_up}")
                cur.execute("DELETE FROM public.activity_logs WHERE chat_id=%s AND month=%s", (group_id, month))
                deleted = cur.rowcount
                if deleted != before_count:
                    raise RuntimeError(f"Delete count mismatch: expected {before_count}, deleted {deleted}")
        context.user_data.pop("pending_reset_month", None)
        await query.edit_message_text(
            f"✅ {month} মাসের {deleted}টি record reset হয়েছে।\n"
            f"Backup table-এ {backed_up}টি record সংরক্ষিত আছে।"
        )
    except Exception:
        logger.exception("Reset failed; transaction rolled back where possible")
        await query.edit_message_text("⚠️ Reset ব্যর্থ হয়েছে। Transaction rollback হয়েছে; Supabase যাচাই করুন।")


# =========================
# /id — admin-only chat ID
# =========================
async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    if not await require_admin(update, context):
        await reject_non_admin(update, context)
        return
    chat = update.effective_chat
    if chat:
        await message.reply_text(f"🆔 Chat ID:\n{chat.id}")


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not found!")
        return
    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable not found!")
        return
    try:
        configured_group_id()
        init_database()
    except Exception:
        logger.exception("Configuration/database initialization failed")
        return

    Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, track_message)
    )
    application.add_handler(CommandHandler("myrank", myrank))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("stat", stat_user))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("backup", backup_data))
    application.add_handler(CommandHandler("resetstats", reset_stats))
    application.add_handler(CallbackQueryHandler(reset_confirmation, pattern=r"^(reset_confirm:\d{4}-\d{2}|reset_cancel)$"))
    application.add_handler(CommandHandler("id", chat_id))

    logger.info("Odvut Activity Bot is running.")
    application.run_polling()


if __name__ == "__main__":
    main()
