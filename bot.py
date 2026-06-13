import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
IST = ZoneInfo("Asia/Kolkata")

# ─── Database ────────────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect("users.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            api_id          INTEGER,
            api_hash        TEXT,
            session         TEXT,
            phone           TEXT,
            password_2fa    TEXT,
            target_bot      TEXT DEFAULT 'FFPlayerLikeBot',
            msg_text        TEXT DEFAULT '/like 0000000000',
            notify_username TEXT DEFAULT NULL,
            task_active     INTEGER DEFAULT 0,
            next_run        TEXT DEFAULT NULL,
            retry_minutes   INTEGER DEFAULT 30,
            phone_code_hash TEXT DEFAULT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS like_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            timestamp   TEXT,
            before      INTEGER,
            after       INTEGER,
            given       INTEGER,
            nickname    TEXT
        )""")
        for col in [
            "ALTER TABLE users ADD COLUMN retry_minutes INTEGER DEFAULT 30",
            "ALTER TABLE users ADD COLUMN notify_username TEXT DEFAULT NULL",
        ]:
            try:
                c.execute(col)
            except:
                pass

init_db()

def get_user(uid):
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def upsert_user(uid, **kwargs):
    with db_conn() as c:
        existing = c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            c.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*kwargs.values(), uid))
        else:
            kwargs["user_id"] = uid
            cols = ", ".join(kwargs.keys())
            qs   = ", ".join("?" * len(kwargs))
            c.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", tuple(kwargs.values()))

def save_like_history(uid, info):
    with db_conn() as c:
        c.execute("""INSERT INTO like_history (user_id, timestamp, before, after, given, nickname)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (uid,
                   datetime.now(IST).isoformat(),
                   int(info.get("before", 0)),
                   int(info.get("after", 0)),
                   int(info.get("given", 0)),
                   info.get("nickname", "")))

def get_history(uid, limit=7):
    with db_conn() as c:
        return c.execute("""SELECT * FROM like_history WHERE user_id=?
                            ORDER BY id DESC LIMIT ?""", (uid, limit)).fetchall()

def get_stats(uid):
    with db_conn() as c:
        total = c.execute("SELECT COUNT(*), SUM(given) FROM like_history WHERE user_id=?", (uid,)).fetchone()
        last  = c.execute("SELECT timestamp FROM like_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        # streak: consecutive days
        rows = c.execute("""SELECT date(timestamp) as d FROM like_history
                            WHERE user_id=? GROUP BY date(timestamp)
                            ORDER BY d DESC""", (uid,)).fetchall()
    count     = total[0] or 0
    total_given = total[1] or 0
    last_time = last["timestamp"] if last else None
    streak = 0
    if rows:
        today = datetime.now(IST).date()
        for i, row in enumerate(rows):
            expected = today - timedelta(days=i)
            if str(row["d"]) == str(expected):
                streak += 1
            else:
                break
    return count, total_given, last_time, streak

# ─── FSM States ──────────────────────────────────────────────────────────────

class LoginStates(StatesGroup):
    api_id    = State()
    api_hash  = State()
    phone     = State()
    otp       = State()
    password  = State()

class SetStates(StatesGroup):
    bot_username    = State()
    message_text    = State()
    retry_minutes   = State()
    notify_username = State()
    schedule_time   = State()

# ─── Keyboards ───────────────────────────────────────────────────────────────

def main_kb(user):
    task_btn  = ("⏹ Stop Task", "stop_task") if user and user["task_active"] else ("▶️ Start Task", "start_task")
    retry_min = user["retry_minutes"]   if user and user["retry_minutes"]   else 30
    notify    = f"@{user['notify_username']}" if user and user["notify_username"] else "Off"
    rows = [
        [InlineKeyboardButton(text=task_btn[0], callback_data=task_btn[1])],
        [
            InlineKeyboardButton(text="🤖 Target Bot", callback_data="set_bot"),
            InlineKeyboardButton(text="✉️ Message",    callback_data="set_msg"),
        ],
        [
            InlineKeyboardButton(text=f"⏱ Retry: {retry_min}m", callback_data="set_retry"),
            InlineKeyboardButton(text=f"🔔 Notify: {notify}",    callback_data="set_notify"),
        ],
        [
            InlineKeyboardButton(text="📊 Stats",    callback_data="stats"),
            InlineKeyboardButton(text="📋 History",  callback_data="history"),
            InlineKeyboardButton(text="🔄 Status",   callback_data="status"),
        ],
    ]
    if not (user and user["session"]):
        rows.insert(0, [InlineKeyboardButton(text="🔑 Login with Telegram", callback_data="login")])
    else:
        rows.append([InlineKeyboardButton(text="🔑 Re-Login", callback_data="login"),
                     InlineKeyboardButton(text="🚪 Logout",   callback_data="logout")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def retry_kb():
    options = [5, 10, 20, 30, 45, 60, 90, 120]
    rows, row = [], []
    for m in options:
        row.append(InlineKeyboardButton(text=f"{m}m", callback_data=f"retry_{m}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Custom", callback_data="retry_custom")])
    rows.append([InlineKeyboardButton(text="❌ Cancel",  callback_data="cancel_retry")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ]])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data="back_main")
    ]])

# ─── Bot & Dispatcher ────────────────────────────────────────────────────────

bot  = Bot(token=BOT_TOKEN)
dp   = Dispatcher(storage=MemoryStorage())
running_tasks: dict[int, asyncio.Task] = {}

# ─── Health check task ───────────────────────────────────────────────────────

async def health_check_loop():
    """Every hour: check if active users got like in last 25h, warn if not."""
    while True:
        await asyncio.sleep(3600)
        try:
            with db_conn() as c:
                rows = c.execute("SELECT user_id FROM users WHERE task_active=1").fetchall()
            for row in rows:
                uid = row["user_id"]
                _, _, last_time, _ = get_stats(uid)
                if last_time:
                    last_dt = datetime.fromisoformat(last_time)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=IST)
                    hours_ago = (datetime.now(IST) - last_dt).total_seconds() / 3600
                    if hours_ago > 25:
                        try:
                            await bot.send_message(uid,
                                f"⚠️ <b>Alert!</b>\n\n"
                                f"24 ghante se zyada ho gaye, like nahi mila!\n"
                                f"Last like: <b>{last_dt.strftime('%d %b %I:%M %p IST')}</b>\n\n"
                                f"Bot chal raha hai, retry kar raha hai.",
                                parse_mode="HTML"
                            )
                        except:
                            pass
        except Exception as e:
            log.error(f"Health check error: {e}")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_client(uid):
    u = get_user(uid)
    return TelegramClient(StringSession(u["session"]), int(u["api_id"]), u["api_hash"])

def next_4am_ist():
    now    = datetime.now(IST)
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target.isoformat()

def seconds_until(iso_str):
    target = datetime.fromisoformat(iso_str)
    if target.tzinfo is None:
        target = target.replace(tzinfo=IST)
    return max((target - datetime.now(IST)).total_seconds(), 0)

def smart_retry_seconds(retry_m: int) -> int:
    """3:50 AM to 10:00 AM IST: retry every 10 min. Otherwise: user setting."""
    now    = datetime.now(IST)
    hour   = now.hour
    minute = now.minute
    # 3:50 AM to 9:59 AM — aggressive retry every 10 min
    if (hour == 3 and minute >= 50) or (4 <= hour <= 9):
        return 10 * 60
    return retry_m * 60

# ─── Response detection ──────────────────────────────────────────────────────

SUCCESS_PATTERNS = [
    r"likes sent successfully",
    r"likes given by bot",
    r"after likes",
    r"daily limit used.*1/1",
]
LIMIT_PATTERNS = [
    r"daily limit reached",
    r"remain count has been exhausted",
    r"already used today",
    r"next reset",
    r"you can try again after reset",
]
COOLDOWN_PATTERN = r"please wait (\d+) seconds"

def detect_response(text: str):
    t = text.lower()
    if any(re.search(p, t) for p in SUCCESS_PATTERNS):
        return "success"
    cd = re.search(COOLDOWN_PATTERN, t)
    if cd:
        return ("cooldown", int(cd.group(1)))
    if any(re.search(p, t) for p in LIMIT_PATTERNS):
        return "limit"
    return "unknown"

def parse_likes(text: str):
    before   = re.search(r"before likes[:\s]+(\d+)", text, re.IGNORECASE)
    after    = re.search(r"after likes[:\s]+(\d+)",  text, re.IGNORECASE)
    given    = re.search(r"likes given by bot[:\s]+(\d+)", text, re.IGNORECASE)
    nickname = re.search(r"player nickname[:\s]+(.+)", text, re.IGNORECASE)
    result   = {}
    if before:   result["before"]   = before.group(1)
    if after:    result["after"]    = after.group(1)
    if given:    result["given"]    = given.group(1)
    if nickname: result["nickname"] = nickname.group(1).strip()
    return result

# ─── Task Loop ───────────────────────────────────────────────────────────────

async def run_task(uid: int):
    log.info(f"[{uid}] Task started")
    while True:
        u = get_user(uid)
        if not u or not u["task_active"]:
            log.info(f"[{uid}] Task stopped")
            break

        # Wait if next_run set (after success)
        if u["next_run"]:
            wait_sec = seconds_until(u["next_run"])
            if wait_sec > 0:
                await asyncio.sleep(min(wait_sec, 60))
                continue
            else:
                upsert_user(uid, next_run=None)

        try:
            client = make_client(uid)
            await client.connect()

            if not await client.is_user_authorized():
                await bot.send_message(uid,
                    "⚠️ <b>Session expire ho gaya!</b>\nDobara /start se login karo.",
                    parse_mode="HTML"
                )
                upsert_user(uid, task_active=0)
                await client.disconnect()
                break

            u        = get_user(uid)
            target   = u["target_bot"]    or "FFPlayerLikeBot"
            msg_text = u["msg_text"]      or "/like 0000000000"
            retry_m  = u["retry_minutes"] or 30
            notify   = u["notify_username"]

            result_holder = {"text": None, "event": asyncio.Event()}

            @client.on(events.NewMessage(incoming=True))
            async def handler(event):
                try:
                    sender = await event.get_sender()
                    sender_username = getattr(sender, 'username', '') or ''
                    sender_first    = getattr(sender, 'first_name', '') or ''
                    # Match by username or first_name (case insensitive)
                    if (target.lower() in sender_username.lower() or
                        target.lower() in sender_first.lower() or
                        sender_username.lower() in target.lower()):
                        if not result_holder["event"].is_set():
                            result_holder["text"] = event.raw_text
                            result_holder["event"].set()
                except Exception:
                    pass

            await client.send_message(target, msg_text)
            log.info(f"[{uid}] Sent → @{target}: {msg_text}")

            try:
                await asyncio.wait_for(result_holder["event"].wait(), timeout=30)
            except asyncio.TimeoutError:
                log.warning(f"[{uid}] No response 30s, retry in 60s")
                await client.disconnect()
                await asyncio.sleep(60)
                continue

            reply_text = result_holder["text"]
            status     = detect_response(reply_text)

            # Cooldown tuple handle
            if isinstance(status, tuple) and status[0] == "cooldown":
                wait_sec = status[1] + 5  # thoda extra buffer
                log.info(f"[{uid}] Cooldown {status[1]}s — waiting {wait_sec}s")
                await client.disconnect()
                await asyncio.sleep(wait_sec)
                continue

            log.info(f"[{uid}] Status: {status}")

            if status == "success":
                info     = parse_likes(reply_text)
                next_run = next_4am_ist()
                upsert_user(uid, next_run=next_run)
                save_like_history(uid, info)

                nr    = datetime.fromisoformat(next_run)
                count, total_given, _, streak = get_stats(uid)

                lines = ["✅ <b>Like Send Ho Gaya!</b>\n"]
                if info.get("nickname"): lines.append(f"👤 Player: <b>{info['nickname']}</b>")
                if info.get("before"):   lines.append(f"📊 Before: <b>{info['before']}</b>")
                if info.get("after"):    lines.append(f"📈 After:  <b>{info['after']}</b>")
                if info.get("given"):    lines.append(f"❤️ Given:  <b>{info['given']}</b>")
                lines.append(f"\n🔥 Streak: <b>{streak} day(s)</b>")
                lines.append(f"📦 Total likes sent: <b>{total_given}</b>")
                lines.append(f"\n😴 Next try: <b>{nr.strftime('%d %b %Y %I:%M %p IST')}</b>")

                success_msg = "\n".join(lines)
                await bot.send_message(uid, success_msg, parse_mode="HTML")

                # Notify target username if set
                if notify:
                    try:
                        notify_text = (
                            f"✅ Like Successfully Sent!\n\n"
                            f"👤 {info.get('nickname','—')}\n"
                            f"📊 Before: {info.get('before','—')} → After: {info.get('after','—')}\n"
                            f"❤️ Given: {info.get('given','—')}\n"
                            f"🔥 Streak: {streak} day(s)"
                        )
                        await client.send_message(notify, notify_text)
                        log.info(f"[{uid}] Notified @{notify}")
                    except Exception as ne:
                        log.warning(f"[{uid}] Notify failed @{notify}: {ne}")

                await client.disconnect()
                # Sleep until next 4 AM — check every 60s for stop signal
                while True:
                    u2 = get_user(uid)
                    if not u2 or not u2["task_active"]:
                        break
                    remaining = seconds_until(next_run)
                    if remaining <= 0:
                        upsert_user(uid, next_run=None)
                        break
                    await asyncio.sleep(min(remaining, 60))

            else:
                # Silent retry — smart interval near 4 AM
                await client.disconnect()
                wait_sec = smart_retry_seconds(retry_m)
                log.info(f"[{uid}] Limit — silent retry in {wait_sec//60}m")
                await asyncio.sleep(wait_sec)

        except Exception as e:
            log.error(f"[{uid}] Error: {e}")
            try:
                await bot.send_message(uid,
                    f"❌ <b>Error:</b>\n<code>{e}</code>\n\n5 min baad retry...",
                    parse_mode="HTML"
                )
            except:
                pass
            await asyncio.sleep(300)

    running_tasks.pop(uid, None)
    log.info(f"[{uid}] Task ended")

def ensure_task(uid):
    if uid not in running_tasks or running_tasks[uid].done():
        running_tasks[uid] = asyncio.create_task(run_task(uid))

# ─── /start ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid  = m.from_user.id
    user = get_user(uid)
    if not user:
        upsert_user(uid)
        user = get_user(uid)

    logged  = bool(user and user["session"])
    active  = bool(user and user["task_active"])
    retry_m = user["retry_minutes"] if user else 30
    notify  = f"@{user['notify_username']}" if user and user["notify_username"] else "Off"
    next_r  = user["next_run"] if user else None
    next_str = datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"

    _, total_given, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"

    await m.answer(
        f"<b>🎮 FF Like Auto-Bot</b>\n\n"
        f"👤 Login: {'🟢 Yes' if logged else '🔴 No'}\n"
        f"⚙️ Task: {'▶️ Running' if active else '⏹ Stopped'}\n"
        f"🤖 Target: <code>@{user['target_bot'] if user else '—'}</code>\n"
        f"✉️ Message: <code>{user['msg_text'] if user else '—'}</code>\n"
        f"⏱ Retry: <b>{retry_m} min</b> (3:50 AM–10 AM: 10 min auto)\n"
        f"🔔 Notify: <b>{notify}</b>\n"
        f"😴 Next run: <b>{next_str}</b>\n\n"
        f"🔥 Streak: <b>{streak} day(s)</b>\n"
        f"📦 Total given: <b>{total_given}</b>\n"
        f"⏰ Last like: <b>{last_str}</b>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )

# ─── Back to main ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = cb.from_user.id
    user = get_user(uid)
    logged  = bool(user and user["session"])
    active  = bool(user and user["task_active"])
    retry_m = user["retry_minutes"] if user else 30
    notify  = f"@{user['notify_username']}" if user and user["notify_username"] else "Off"
    next_r  = user["next_run"] if user else None
    next_str = datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"
    _, total_given, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"

    try:
        await cb.message.edit_text(
            f"<b>🎮 FF Like Auto-Bot</b>\n\n"
            f"👤 Login: {'🟢 Yes' if logged else '🔴 No'}\n"
            f"⚙️ Task: {'▶️ Running' if active else '⏹ Stopped'}\n"
            f"🤖 Target: <code>@{user['target_bot'] if user else '—'}</code>\n"
            f"✉️ Message: <code>{user['msg_text'] if user else '—'}</code>\n"
            f"⏱ Retry: <b>{retry_m} min</b>\n"
            f"🔔 Notify: <b>{notify}</b>\n"
            f"😴 Next run: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak} day(s)</b>\n"
            f"📦 Total given: <b>{total_given}</b>\n"
            f"⏰ Last like: <b>{last_str}</b>",
            parse_mode="HTML",
            reply_markup=main_kb(user)
        )
    except Exception:
        pass
    await cb.answer()

# ─── Status ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    logged  = bool(user and user["session"])
    active  = bool(user and user["task_active"])
    retry_m = user["retry_minutes"] if user else 30
    notify  = f"@{user['notify_username']}" if user and user["notify_username"] else "Off"
    next_r  = user["next_run"] if user else None
    next_str = datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"
    _, total_given, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"

    try:
        await cb.message.edit_text(
            f"<b>🔄 Status</b>\n\n"
            f"🔑 Login: {'✅' if logged else '❌'}\n"
            f"⚙️ Task: {'🟢 Running' if active else '🔴 Stopped'}\n"
            f"🤖 Target: <code>@{user['target_bot'] if user else '—'}</code>\n"
            f"✉️ Message: <code>{user['msg_text'] if user else '—'}</code>\n"
            f"⏱ Retry: <b>{retry_m} min</b>\n"
            f"🔔 Notify: <b>{notify}</b>\n"
            f"😴 Next run: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak} day(s)</b>\n"
            f"📦 Total given: <b>{total_given}</b>\n"
            f"⏰ Last like: <b>{last_str}</b>",
            parse_mode="HTML",
            reply_markup=main_kb(user)
        )
    except Exception:
        pass
    await cb.answer("🔄 Refreshed!")

# ─── Stats ───────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    uid = cb.from_user.id
    count, total_given, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %Y %I:%M %p IST") if last_time else "—"

    try:
        await cb.message.edit_text(
            f"<b>📊 Your Stats</b>\n\n"
            f"🔥 Current Streak: <b>{streak} day(s)</b>\n"
            f"📅 Total Days: <b>{count}</b>\n"
            f"❤️ Total Likes Sent: <b>{total_given}</b>\n"
            f"⏰ Last Like: <b>{last_str}</b>",
            parse_mode="HTML",
            reply_markup=back_kb()
        )
    except Exception:
        pass
    await cb.answer()

# ─── History ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    uid  = cb.from_user.id
    rows = get_history(uid, limit=7)

    if not rows:
        try:
            await cb.message.edit_text(
                "📋 <b>History</b>\n\nAbhi tak koi like nahi mila.",
                parse_mode="HTML", reply_markup=back_kb()
            )
        except Exception:
            pass
        await cb.answer()
        return

    lines = ["<b>📋 Last 7 Likes</b>\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        lines.append(
            f"📅 <b>{dt.strftime('%d %b %I:%M %p')}</b>\n"
            f"   👤 {r['nickname'] or '—'} | "
            f"Before: {r['before']} → After: {r['after']} (+{r['given']})\n"
        )

    try:
        await cb.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=back_kb()
        )
    except Exception:
        pass
    await cb.answer()

# ─── Start / Stop ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "start_task")
async def cb_start_task(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    if not user or not user["session"]:
        await cb.answer("⚠️ Pehle login karo!", show_alert=True)
        return
    upsert_user(uid, task_active=1)
    ensure_task(uid)
    try:
        await cb.message.edit_reply_markup(reply_markup=main_kb(get_user(uid)))
    except Exception:
        pass
    await cb.answer("✅ Task started!")

@dp.callback_query(F.data == "stop_task")
async def cb_stop_task(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, task_active=0)
    try:
        await cb.message.edit_reply_markup(reply_markup=main_kb(get_user(uid)))
    except Exception:
        pass
    await cb.answer("⏹ Task stopped!")

# ─── Set Notify ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_notify")
async def cb_set_notify(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.notify_username)
    await cb.message.answer(
        "🔔 <b>Notify Username</b>\n\n"
        "Like milne pe kise message bhejun?\n"
        "Username bhejo (without @): <code>myusername</code>\n\n"
        "Band karna ho to: <code>none</code>",
        parse_mode="HTML", reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.notify_username)
async def set_notify_username(m: Message, state: FSMContext):
    val = m.text.strip().lstrip("@")
    if val.lower() == "none":
        upsert_user(m.from_user.id, notify_username=None)
        await state.clear()
        await m.answer("🔕 Notify band.", reply_markup=main_kb(get_user(m.from_user.id)))
    else:
        upsert_user(m.from_user.id, notify_username=val)
        await state.clear()
        await m.answer(f"✅ Notify: <code>@{val}</code>",
                       parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

# ─── Set Retry ───────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_retry")
async def cb_set_retry(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    curr = user["retry_minutes"] if user else 30
    await cb.message.answer(
        f"⏱ <b>Retry Interval</b>\n\nCurrent: <b>{curr} min</b>\n\n"
        f"Limit pe kitne min baad retry kare?\n"
        f"<i>Note: 3:50 AM–10:00 AM ke beech automatically 10 min retry hoga.</i>",
        parse_mode="HTML", reply_markup=retry_kb()
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("retry_") & ~F.data.in_({"retry_custom"}))
async def cb_retry_select(cb: CallbackQuery):
    minutes = int(cb.data.split("_")[1])
    upsert_user(cb.from_user.id, retry_minutes=minutes)
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.message.answer(f"✅ Retry: <b>{minutes} min</b>",
                             parse_mode="HTML", reply_markup=main_kb(get_user(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "retry_custom")
async def cb_retry_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.retry_minutes)
    await cb.message.answer("✏️ Minutes daalo (1-1440):", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(SetStates.retry_minutes)
async def set_retry_minutes(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not (1 <= int(m.text.strip()) <= 1440):
        await m.answer("❌ 1 se 1440 ke beech number daalo:")
        return
    minutes = int(m.text.strip())
    upsert_user(m.from_user.id, retry_minutes=minutes)
    await state.clear()
    await m.answer(f"✅ Retry: <b>{minutes} min</b>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

@dp.callback_query(F.data == "cancel_retry")
async def cb_cancel_retry(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Cancelled")

# ─── Set Target Bot ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_bot")
async def cb_set_bot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.bot_username)
    await cb.message.answer(
        "🤖 Target bot username (without @):\nExample: <code>FFPlayerLikeBot</code>",
        parse_mode="HTML", reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.bot_username)
async def set_bot_username(m: Message, state: FSMContext):
    upsert_user(m.from_user.id, target_bot=m.text.strip().lstrip("@"))
    await state.clear()
    await m.answer(f"✅ Target: <code>@{m.text.strip().lstrip('@')}</code>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

# ─── Set Message ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_msg")
async def cb_set_msg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.message_text)
    await cb.message.answer(
        "✉️ Message bhejo:\nExample: <code>/like 1902086798</code>",
        parse_mode="HTML", reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.message_text)
async def set_message_text(m: Message, state: FSMContext):
    upsert_user(m.from_user.id, msg_text=m.text.strip())
    await state.clear()
    await m.answer(f"✅ Message: <code>{m.text.strip()}</code>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

# ─── Cancel ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Cancelled")

# ─── Logout ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, session="", task_active=0)
    try:
        await cb.message.edit_text("🚪 Logged out.", reply_markup=main_kb(get_user(uid)))
    except Exception:
        pass
    await cb.answer()

# ─── Login Flow ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "login")
async def cb_login(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LoginStates.api_id)
    await cb.message.answer(
        "🔑 <b>Login — Step 1/5</b>\n\n"
        "Apna <b>API ID</b> bhejo.\n👉 my.telegram.org → App API",
        parse_mode="HTML", reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(LoginStates.api_id)
async def login_api_id(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("❌ API ID sirf numbers hota hai:")
        return
    await state.update_data(api_id=int(m.text.strip()))
    await state.set_state(LoginStates.api_hash)
    await m.answer("🔑 <b>Step 2/5</b> — <b>API Hash</b> bhejo:",
                   parse_mode="HTML", reply_markup=cancel_kb())

@dp.message(LoginStates.api_hash)
async def login_api_hash(m: Message, state: FSMContext):
    await state.update_data(api_hash=m.text.strip())
    await state.set_state(LoginStates.phone)
    await m.answer(
        "🔑 <b>Step 3/5</b> — Phone number (country code ke saath):\n"
        "Example: <code>+919876543210</code>",
        parse_mode="HTML", reply_markup=cancel_kb()
    )

@dp.message(LoginStates.phone)
async def login_phone(m: Message, state: FSMContext):
    data  = await state.get_data()
    phone = m.text.strip()
    await state.update_data(phone=phone)
    try:
        client = TelegramClient(StringSession(), data["api_id"], data["api_hash"])
        await client.connect()
        result = await client.send_code_request(phone)
        await state.update_data(
            phone_code_hash=result.phone_code_hash,
            session_str=client.session.save()
        )
        await client.disconnect()
        await state.set_state(LoginStates.otp)
        await m.answer(
            "🔑 <b>Step 4/5</b> — OTP bhejo\n"
            "Format: <code>2 3 4 5 6</code> ya <code>23456</code>",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.otp)
async def login_otp(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    otp  = m.text.strip().replace(" ", "")
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        try:
            await client.sign_in(
                phone=data["phone"],
                code=otp,
                phone_code_hash=data["phone_code_hash"]
            )
            session_str = client.session.save()
            await client.disconnect()
            upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                        phone=data["phone"], session=session_str, task_active=0)
            await state.clear()
            await m.answer("✅ <b>Login ho gaya!</b>\nAb ▶️ Start Task dabao.",
                           parse_mode="HTML", reply_markup=main_kb(get_user(uid)))
        except Exception as e:
            err = str(e)
            if "SessionPasswordNeeded" in err or "password" in err.lower() or "2FA" in err:
                await state.update_data(session_str=client.session.save())
                await client.disconnect()
                await state.set_state(LoginStates.password)
                await m.answer(
                    "🔑 <b>Step 5/5</b> — 2FA Password bhejo:\n\n"
                    "⚠️ Exactly wahi password jo Telegram mein set hai.",
                    parse_mode="HTML", reply_markup=cancel_kb()
                )
            else:
                await client.disconnect()
                await state.clear()
                await m.answer(f"❌ OTP Error: <code>{e}</code>", parse_mode="HTML")
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.password)
async def login_password(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    pwd  = m.text.strip()
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        await client(GetPasswordRequest())
        await client.sign_in(password=pwd)
        session_str = client.session.save()
        await client.disconnect()
        upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                    phone=data["phone"], session=session_str,
                    password_2fa=pwd, task_active=0)
        await state.clear()
        await m.answer("✅ <b>Login ho gaya!</b>\nAb ▶️ Start Task dabao.",
                       parse_mode="HTML", reply_markup=main_kb(get_user(uid)))
    except Exception as e:
        await state.clear()
        await m.answer(
            f"❌ <b>Password galat hai!</b>\n<code>{e}</code>\n\n"
            f"Dobara /start se try karo.",
            parse_mode="HTML"
        )

# ─── Resume + Health check on startup ────────────────────────────────────────

async def resume_tasks():
    with db_conn() as c:
        rows = c.execute("SELECT user_id FROM users WHERE task_active=1").fetchall()
    for row in rows:
        log.info(f"Resuming task for {row['user_id']}")
        ensure_task(row["user_id"])

async def main():
    await resume_tasks()
    asyncio.create_task(health_check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
