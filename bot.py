import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient
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

BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
IST        = ZoneInfo("Asia/Kolkata")

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
            username        TEXT DEFAULT NULL,
            first_name      TEXT DEFAULT NULL,
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
            is_banned       INTEGER DEFAULT 0,
            joined_at       TEXT DEFAULT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS like_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            timestamp TEXT,
            before    INTEGER,
            after     INTEGER,
            given     INTEGER,
            nickname  TEXT
        )""")
        for col in [
            "ALTER TABLE users ADD COLUMN retry_minutes INTEGER DEFAULT 30",
            "ALTER TABLE users ADD COLUMN notify_username TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN username TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN joined_at TEXT DEFAULT NULL",
        ]:
            try: c.execute(col)
            except: pass

init_db()

def get_user(uid):
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def get_all_users():
    with db_conn() as c:
        return c.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()

def upsert_user(uid, **kwargs):
    with db_conn() as c:
        existing = c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            c.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*kwargs.values(), uid))
        else:
            kwargs["user_id"]   = uid
            kwargs.setdefault("joined_at", datetime.now(IST).isoformat())
            cols = ", ".join(kwargs.keys())
            qs   = ", ".join("?" * len(kwargs))
            c.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", tuple(kwargs.values()))

def save_like_history(uid, info):
    with db_conn() as c:
        c.execute("""INSERT INTO like_history (user_id,timestamp,before,after,given,nickname)
                     VALUES (?,?,?,?,?,?)""",
                  (uid, datetime.now(IST).isoformat(),
                   int(info.get("before",0)), int(info.get("after",0)),
                   int(info.get("given",0)), info.get("nickname","")))

def get_history(uid, limit=7):
    with db_conn() as c:
        return c.execute("""SELECT * FROM like_history WHERE user_id=?
                            ORDER BY id DESC LIMIT ?""", (uid, limit)).fetchall()

def get_stats(uid):
    with db_conn() as c:
        total = c.execute("SELECT COUNT(*), SUM(given) FROM like_history WHERE user_id=?", (uid,)).fetchone()
        last  = c.execute("SELECT timestamp FROM like_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        rows  = c.execute("""SELECT date(timestamp) as d FROM like_history
                             WHERE user_id=? GROUP BY date(timestamp)
                             ORDER BY d DESC""", (uid,)).fetchall()
    streak = 0
    if rows:
        today = datetime.now(IST).date()
        for i, row in enumerate(rows):
            if str(row["d"]) == str(today - timedelta(days=i)):
                streak += 1
            else:
                break
    return (total[0] or 0), (total[1] or 0), (last["timestamp"] if last else None), streak

# ─── FSM ─────────────────────────────────────────────────────────────────────

class LoginStates(StatesGroup):
    api_id   = State()
    api_hash = State()
    phone    = State()
    otp      = State()
    password = State()

class SetStates(StatesGroup):
    bot_username    = State()
    message_text    = State()
    retry_minutes   = State()
    notify_username = State()

class AdminStates(StatesGroup):
    broadcast       = State()
    edit_target_bot = State()
    edit_msg        = State()
    edit_retry      = State()

# ─── Keyboards ───────────────────────────────────────────────────────────────

def main_kb(user):
    task_btn  = ("⏹ Stop Task","stop_task") if user and user["task_active"] else ("▶️ Start Task","start_task")
    retry_min = (user["retry_minutes"] if user and user["retry_minutes"] else 30)
    notify    = f"@{user['notify_username']}" if user and user["notify_username"] else "Off"
    rows = [
        [InlineKeyboardButton(text=task_btn[0], callback_data=task_btn[1])],
        [InlineKeyboardButton(text="🤖 Target Bot", callback_data="set_bot"),
         InlineKeyboardButton(text="✉️ Message",    callback_data="set_msg")],
        [InlineKeyboardButton(text=f"⏱ Retry: {retry_min}m", callback_data="set_retry"),
         InlineKeyboardButton(text=f"🔔 Notify: {notify}",   callback_data="set_notify")],
        [InlineKeyboardButton(text="📊 Stats",   callback_data="stats"),
         InlineKeyboardButton(text="📋 History", callback_data="history"),
         InlineKeyboardButton(text="🔄 Status",  callback_data="status")],
    ]
    if not (user and user["session"]):
        rows.insert(0,[InlineKeyboardButton(text="🔑 Login with Telegram", callback_data="login")])
    else:
        rows.append([InlineKeyboardButton(text="🔑 Re-Login", callback_data="login"),
                     InlineKeyboardButton(text="🚪 Logout",   callback_data="logout")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def retry_kb():
    options = [5,10,20,30,45,60,90,120]
    rows, row = [], []
    for m in options:
        row.append(InlineKeyboardButton(text=f"{m}m", callback_data=f"retry_{m}"))
        if len(row)==4: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Custom", callback_data="retry_custom")])
    rows.append([InlineKeyboardButton(text="❌ Cancel",  callback_data="cancel_retry")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data="back_main")]])

def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users List",      callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Broadcast",       callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Global Stats",    callback_data="admin_gstats")],
    ])

def admin_users_kb(users, page=0, per_page=5):
    start = page * per_page
    chunk = users[start:start+per_page]
    rows  = []
    for u in chunk:
        name   = u["first_name"] or u["username"] or str(u["user_id"])
        active = "🟢" if u["task_active"] else "🔴"
        banned = "🚫" if u["is_banned"]   else ""
        rows.append([InlineKeyboardButton(
            text=f"{active}{banned} {name}",
            callback_data=f"admin_user_{u['user_id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"admin_page_{page-1}"))
    if start + per_page < len(users):
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"admin_page_{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_user_detail_kb(uid, user):
    task_lbl = "⏹ Stop Task" if user["task_active"] else "▶️ Start Task"
    task_cb  = f"admin_stop_{uid}" if user["task_active"] else f"admin_start_{uid}"
    ban_lbl  = "✅ Unban" if user["is_banned"] else "🚫 Ban"
    ban_cb   = f"admin_unban_{uid}" if user["is_banned"] else f"admin_ban_{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=task_lbl,          callback_data=task_cb)],
        [InlineKeyboardButton(text="🤖 Edit Target",  callback_data=f"admin_ebot_{uid}"),
         InlineKeyboardButton(text="✉️ Edit Msg",     callback_data=f"admin_emsg_{uid}")],
        [InlineKeyboardButton(text="⏱ Edit Retry",   callback_data=f"admin_eretry_{uid}"),
         InlineKeyboardButton(text=ban_lbl,           callback_data=ban_cb)],
        [InlineKeyboardButton(text="📋 History",      callback_data=f"admin_hist_{uid}")],
        [InlineKeyboardButton(text="🔙 Users List",   callback_data="admin_users")],
    ])

# ─── Bot & Dispatcher ────────────────────────────────────────────────────────

bot  = Bot(token=BOT_TOKEN)
dp   = Dispatcher(storage=MemoryStorage())
running_tasks: dict[int, asyncio.Task] = {}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_admin(uid): return uid == ADMIN_ID

def make_client(uid):
    u = get_user(uid)
    return TelegramClient(StringSession(u["session"]), int(u["api_id"]), u["api_hash"])

def next_4am_ist():
    now = datetime.now(IST)
    t   = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= t: t += timedelta(days=1)
    return t.isoformat()

def seconds_until(iso_str):
    t = datetime.fromisoformat(iso_str)
    if t.tzinfo is None: t = t.replace(tzinfo=IST)
    return max((t - datetime.now(IST)).total_seconds(), 0)

def smart_retry_seconds(retry_m):
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    if (h==3 and m>=50) or (4<=h<=9):
        return 10*60
    return retry_m*60

KEYWORDS = ["likes sent","daily limit","please wait","like request",
            "remain count","next reset","likes given","before likes","after likes"]

SUCCESS_PATTERNS = [r"likes sent successfully",r"likes given by bot",r"after likes",r"daily limit used.*1/1"]
LIMIT_PATTERNS   = [r"daily limit reached",r"remain count has been exhausted",
                    r"already used today",r"next reset",r"you can try again after reset"]
COOLDOWN_PAT     = r"please wait (\d+) seconds"

def detect_response(text):
    t = text.lower()
    if any(re.search(p,t) for p in SUCCESS_PATTERNS): return "success"
    cd = re.search(COOLDOWN_PAT, t)
    if cd: return ("cooldown", int(cd.group(1)))
    if any(re.search(p,t) for p in LIMIT_PATTERNS): return "limit"
    return "unknown"

def parse_likes(text):
    result = {}
    for key, pat in [("before",r"before likes[:\s]+(\d+)"),("after",r"after likes[:\s]+(\d+)"),
                     ("given",r"likes given by bot[:\s]+(\d+)"),("nickname",r"player nickname[:\s]+(.+)")]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: result[key] = m.group(1).strip()
    return result

# ─── Task Loop ───────────────────────────────────────────────────────────────

async def run_task(uid: int):
    log.info(f"[{uid}] Task started")
    while True:
        u = get_user(uid)
        if not u or not u["task_active"]:
            log.info(f"[{uid}] Task stopped"); break
        if u["is_banned"]:
            log.info(f"[{uid}] Banned — stopping"); break

        if u["next_run"]:
            wait_sec = seconds_until(u["next_run"])
            if wait_sec > 0:
                await asyncio.sleep(min(wait_sec, 60)); continue
            else:
                upsert_user(uid, next_run=None)

        try:
            client = make_client(uid)
            await client.connect()
            if not await client.is_user_authorized():
                await bot.send_message(uid,"⚠️ <b>Session expire!</b> Dobara /start se login karo.",parse_mode="HTML")
                upsert_user(uid, task_active=0)
                await client.disconnect(); break

            u        = get_user(uid)
            target   = u["target_bot"]    or "FFPlayerLikeBot"
            msg_text = u["msg_text"]      or "/like 0000000000"
            retry_m  = u["retry_minutes"] or 30
            notify   = u["notify_username"]

            sent_at = datetime.now(IST)
            await client.send_message(target, msg_text)
            log.info(f"[{uid}] Sent → @{target}: {msg_text} at {sent_at.strftime('%H:%M:%S')}")

            # Poll for response — only accept messages AFTER sent_at
            reply_text = None
            for _ in range(15):
                await asyncio.sleep(2)
                try:
                    async for msg in client.iter_messages(target, limit=5):
                        # Skip messages older than when we sent
                        msg_time = msg.date
                        if hasattr(msg_time, 'astimezone'):
                            msg_time = msg_time.astimezone(IST)
                        else:
                            from datetime import timezone
                            msg_time = msg_time.replace(tzinfo=timezone.utc).astimezone(IST)
                        
                        if msg_time < sent_at:
                            continue  # Purana message — skip!
                        
                        text = msg.raw_text or ""
                        if any(k in text.lower() for k in KEYWORDS):
                            reply_text = text
                            log.info(f"[{uid}] Got (at {msg_time.strftime('%H:%M:%S')}): {text[:80]}")
                            break
                    if reply_text: break
                except Exception as e:
                    log.warning(f"[{uid}] Poll: {e}")

            if not reply_text:
                log.warning(f"[{uid}] No response, retry 60s")
                await client.disconnect()
                await asyncio.sleep(60); continue

            status = detect_response(reply_text)

            if isinstance(status, tuple) and status[0]=="cooldown":
                log.info(f"[{uid}] Cooldown {status[1]}s")
                await client.disconnect()
                await asyncio.sleep(status[1]+5); continue

            log.info(f"[{uid}] Status: {status}")

            if status == "success":
                info     = parse_likes(reply_text)
                next_run = next_4am_ist()
                upsert_user(uid, next_run=next_run)
                save_like_history(uid, info)
                nr = datetime.fromisoformat(next_run)
                _, total_given, _, streak = get_stats(uid)

                lines = ["✅ <b>Like Send Ho Gaya!</b>\n"]
                if info.get("nickname"): lines.append(f"👤 Player: <b>{info['nickname']}</b>")
                if info.get("before"):   lines.append(f"📊 Before: <b>{info['before']}</b>")
                if info.get("after"):    lines.append(f"📈 After:  <b>{info['after']}</b>")
                if info.get("given"):    lines.append(f"❤️ Given:  <b>{info['given']}</b>")
                lines.append(f"\n🔥 Streak: <b>{streak} day(s)</b>")
                lines.append(f"📦 Total: <b>{total_given}</b>")
                lines.append(f"\n😴 Next: <b>{nr.strftime('%d %b %Y %I:%M %p IST')}</b>")
                await bot.send_message(uid, "\n".join(lines), parse_mode="HTML")

                if notify:
                    try:
                        await client.send_message(notify,
                            f"✅ Like Sent!\n👤 {info.get('nickname','—')}\n"
                            f"📊 {info.get('before','—')}→{info.get('after','—')} (+{info.get('given','—')})\n"
                            f"🔥 Streak: {streak}d")
                    except Exception as ne:
                        log.warning(f"[{uid}] Notify fail: {ne}")

                # Notify admin too
                if ADMIN_ID and uid != ADMIN_ID:
                    try:
                        u2 = get_user(uid)
                        name = u2["first_name"] or u2["username"] or str(uid)
                        await bot.send_message(ADMIN_ID,
                            f"✅ <b>Like Sent</b> — {name}\n"
                            f"👤 {info.get('nickname','—')} | +{info.get('given','—')}", parse_mode="HTML")
                    except: pass

                await client.disconnect()
                while True:
                    u2 = get_user(uid)
                    if not u2 or not u2["task_active"]: break
                    rem = seconds_until(next_run)
                    if rem <= 0:
                        upsert_user(uid, next_run=None); break
                    await asyncio.sleep(min(rem, 60))

            else:
                await client.disconnect()
                wait_sec = smart_retry_seconds(retry_m)
                log.info(f"[{uid}] Limit — retry {wait_sec//60}m")
                await asyncio.sleep(wait_sec)

        except Exception as e:
            log.error(f"[{uid}] Error: {e}")
            try:
                await bot.send_message(uid,f"❌ <b>Error:</b>\n<code>{e}</code>\n\n5 min retry...",parse_mode="HTML")
            except: pass
            await asyncio.sleep(300)

    running_tasks.pop(uid, None)
    log.info(f"[{uid}] Task ended")

def ensure_task(uid):
    if uid not in running_tasks or running_tasks[uid].done():
        running_tasks[uid] = asyncio.create_task(run_task(uid))

# ─── Health Check ────────────────────────────────────────────────────────────

async def health_check_loop():
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
                    if last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=IST)
                    if (datetime.now(IST)-last_dt).total_seconds()/3600 > 25:
                        try:
                            await bot.send_message(uid,
                                f"⚠️ 24h+ ho gaye, like nahi mila!\n"
                                f"Last: {last_dt.strftime('%d %b %I:%M %p IST')}", parse_mode="HTML")
                        except: pass
        except Exception as e:
            log.error(f"Health: {e}")

# ─── /start ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid  = m.from_user.id
    user = get_user(uid)
    if not user:
        upsert_user(uid,
            username=m.from_user.username,
            first_name=m.from_user.first_name)
        user = get_user(uid)
    else:
        upsert_user(uid,
            username=m.from_user.username,
            first_name=m.from_user.first_name)
        user = get_user(uid)

    if user["is_banned"]:
        await m.answer("🚫 Aap banned hain. Admin se contact karo.")
        return

    logged  = bool(user["session"])
    active  = bool(user["task_active"])
    retry_m = user["retry_minutes"] or 30
    notify  = f"@{user['notify_username']}" if user["notify_username"] else "Off"
    next_r  = user["next_run"]
    next_str= datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"
    _, total_given, last_time, streak = get_stats(uid)
    last_str= datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"

    text = (f"<b>🎮 FF Like Auto-Bot</b>\n\n"
            f"👤 Login: {'🟢' if logged else '🔴'}\n"
            f"⚙️ Task: {'▶️ Running' if active else '⏹ Stopped'}\n"
            f"🤖 Target: <code>@{user['target_bot']}</code>\n"
            f"✉️ Message: <code>{user['msg_text']}</code>\n"
            f"⏱ Retry: <b>{retry_m}m</b> (3:50–10AM → 10m auto)\n"
            f"🔔 Notify: <b>{notify}</b>\n"
            f"😴 Next run: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak} day(s)</b>\n"
            f"📦 Total given: <b>{total_given}</b>\n"
            f"⏰ Last like: <b>{last_str}</b>")

    if is_admin(uid):
        text += "\n\n<b>⚡ Admin Panel: /admin</b>"

    await m.answer(text, parse_mode="HTML", reply_markup=main_kb(user))

# ═══════════════════════════════════════════════════════
# ─── ADMIN PANEL ────────────────────────────────────────
# ═══════════════════════════════════════════════════════

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("❌ Access denied."); return
    users = get_all_users()
    active = sum(1 for u in users if u["task_active"])
    banned = sum(1 for u in users if u["is_banned"])
    await m.answer(
        f"<b>⚡ Admin Panel</b>\n\n"
        f"👥 Total users: <b>{len(users)}</b>\n"
        f"🟢 Active tasks: <b>{active}</b>\n"
        f"🚫 Banned: <b>{banned}</b>",
        parse_mode="HTML",
        reply_markup=admin_main_kb()
    )

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    users  = get_all_users()
    active = sum(1 for u in users if u["task_active"])
    banned = sum(1 for u in users if u["is_banned"])
    try:
        await cb.message.edit_text(
            f"<b>⚡ Admin Panel</b>\n\n"
            f"👥 Total: <b>{len(users)}</b>\n"
            f"🟢 Active: <b>{active}</b>\n"
            f"🚫 Banned: <b>{banned}</b>",
            parse_mode="HTML", reply_markup=admin_main_kb()
        )
    except: pass
    await cb.answer()

# ── Users List ───────────────────────────────────────────

@dp.callback_query(F.data == "admin_users")
async def cb_admin_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    users = get_all_users()
    try:
        await cb.message.edit_text(
            f"<b>👥 Users ({len(users)})</b>\n🟢=Active 🔴=Stopped 🚫=Banned",
            parse_mode="HTML",
            reply_markup=admin_users_kb(users, page=0)
        )
    except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("admin_page_"))
async def cb_admin_page(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    page  = int(cb.data.split("_")[2])
    users = get_all_users()
    try:
        await cb.message.edit_reply_markup(reply_markup=admin_users_kb(users, page=page))
    except: pass
    await cb.answer()

# ── User Detail ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("admin_user_"))
async def cb_admin_user_detail(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid  = int(cb.data.split("_")[2])
    user = get_user(uid)
    if not user:
        await cb.answer("User not found!"); return

    _, total, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"
    next_str = datetime.fromisoformat(user["next_run"]).strftime("%d %b %I:%M %p IST") if user["next_run"] else "—"
    name = user["first_name"] or user["username"] or str(uid)

    try:
        await cb.message.edit_text(
            f"<b>👤 {name}</b> (<code>{uid}</code>)\n\n"
            f"📱 Phone: <code>{user['phone'] or '—'}</code>\n"
            f"🤖 Target: <code>@{user['target_bot']}</code>\n"
            f"✉️ Msg: <code>{user['msg_text']}</code>\n"
            f"⏱ Retry: <b>{user['retry_minutes']}m</b>\n"
            f"⚙️ Task: {'🟢 Running' if user['task_active'] else '🔴 Stopped'}\n"
            f"🚫 Banned: {'Yes' if user['is_banned'] else 'No'}\n"
            f"😴 Next run: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak}d</b> | 📦 Total: <b>{total}</b>\n"
            f"⏰ Last like: <b>{last_str}</b>",
            parse_mode="HTML",
            reply_markup=admin_user_detail_kb(uid, user)
        )
    except: pass
    await cb.answer()

# ── Force Start/Stop ─────────────────────────────────────

@dp.callback_query(F.data.startswith("admin_start_"))
async def cb_admin_force_start(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid  = int(cb.data.split("_")[2])
    user = get_user(uid)
    if not user or not user["session"]:
        await cb.answer("❌ User ne login nahi kiya!", show_alert=True); return
    if user["is_banned"]:
        await cb.answer("❌ User banned hai!", show_alert=True); return
    upsert_user(uid, task_active=1)
    ensure_task(uid)
    try:
        await bot.send_message(uid, "⚡ Admin ne aapka task start kar diya!")
    except: pass
    await cb.answer("✅ Task started!")
    await cb_admin_user_detail(cb)

@dp.callback_query(F.data.startswith("admin_stop_"))
async def cb_admin_force_stop(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    upsert_user(uid, task_active=0)
    try:
        await bot.send_message(uid, "⚡ Admin ne aapka task stop kar diya!")
    except: pass
    await cb.answer("⏹ Task stopped!")
    await cb_admin_user_detail(cb)

# ── Ban/Unban ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("admin_ban_"))
async def cb_admin_ban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    upsert_user(uid, is_banned=1, task_active=0)
    try:
        await bot.send_message(uid, "🚫 Aap banned ho gaye hain. Admin se contact karo.")
    except: pass
    await cb.answer("🚫 Banned!")
    await cb_admin_user_detail(cb)

@dp.callback_query(F.data.startswith("admin_unban_"))
async def cb_admin_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    upsert_user(uid, is_banned=0)
    try:
        await bot.send_message(uid, "✅ Aap unban ho gaye hain!")
    except: pass
    await cb.answer("✅ Unbanned!")
    await cb_admin_user_detail(cb)

# ── Admin Edit Target Bot ────────────────────────────────

@dp.callback_query(F.data.startswith("admin_ebot_"))
async def cb_admin_edit_bot(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    await state.set_state(AdminStates.edit_target_bot)
    await state.update_data(edit_uid=uid)
    await cb.message.answer(
        f"🤖 User <code>{uid}</code> ka target bot username bhejo (without @):",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminStates.edit_target_bot)
async def admin_set_target_bot(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data()
    uid  = data["edit_uid"]
    upsert_user(uid, target_bot=m.text.strip().lstrip("@"))
    await state.clear()
    await m.answer(f"✅ User {uid} ka target bot set: <code>@{m.text.strip().lstrip('@')}</code>",
                   parse_mode="HTML")

# ── Admin Edit Message ───────────────────────────────────

@dp.callback_query(F.data.startswith("admin_emsg_"))
async def cb_admin_edit_msg(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    await state.set_state(AdminStates.edit_msg)
    await state.update_data(edit_uid=uid)
    await cb.message.answer(
        f"✉️ User <code>{uid}</code> ka message bhejo:",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminStates.edit_msg)
async def admin_set_msg(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data()
    uid  = data["edit_uid"]
    upsert_user(uid, msg_text=m.text.strip())
    await state.clear()
    await m.answer(f"✅ User {uid} ka message set: <code>{m.text.strip()}</code>",
                   parse_mode="HTML")

# ── Admin Edit Retry ─────────────────────────────────────

@dp.callback_query(F.data.startswith("admin_eretry_"))
async def cb_admin_edit_retry(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split("_")[2])
    await state.set_state(AdminStates.edit_retry)
    await state.update_data(edit_uid=uid)
    await cb.message.answer(
        f"⏱ User <code>{uid}</code> ka retry minutes bhejo (1-1440):",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminStates.edit_retry)
async def admin_set_retry(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    if not m.text.strip().isdigit() or not (1 <= int(m.text.strip()) <= 1440):
        await m.answer("❌ 1-1440 ke beech number daalo:"); return
    data = await state.get_data()
    uid  = data["edit_uid"]
    upsert_user(uid, retry_minutes=int(m.text.strip()))
    await state.clear()
    await m.answer(f"✅ User {uid} ka retry: <b>{m.text.strip()} min</b>", parse_mode="HTML")

# ── Admin History ────────────────────────────────────────

@dp.callback_query(F.data.startswith("admin_hist_"))
async def cb_admin_hist(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid  = int(cb.data.split("_")[2])
    rows = get_history(uid, limit=5)
    if not rows:
        await cb.answer("No history!", show_alert=True); return
    lines = [f"<b>📋 History — {uid}</b>\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        lines.append(f"📅 {dt.strftime('%d %b %I:%M %p')}\n"
                     f"   {r['nickname'] or '—'} | {r['before']}→{r['after']} (+{r['given']})\n")
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                       InlineKeyboardButton(text="🔙 Back", callback_data=f"admin_user_{uid}")]]))
    except: pass
    await cb.answer()

# ── Global Stats ─────────────────────────────────────────

@dp.callback_query(F.data == "admin_gstats")
async def cb_admin_gstats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    users = get_all_users()
    with db_conn() as c:
        total_likes = c.execute("SELECT SUM(given) FROM like_history").fetchone()[0] or 0
        total_days  = c.execute("SELECT COUNT(*) FROM like_history").fetchone()[0] or 0
    active = sum(1 for u in users if u["task_active"])
    logged = sum(1 for u in users if u["session"])
    try:
        await cb.message.edit_text(
            f"<b>📊 Global Stats</b>\n\n"
            f"👥 Total users: <b>{len(users)}</b>\n"
            f"🔑 Logged in: <b>{logged}</b>\n"
            f"🟢 Active tasks: <b>{active}</b>\n"
            f"❤️ Total likes sent: <b>{total_likes}</b>\n"
            f"📅 Total like events: <b>{total_days}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")]])
        )
    except: pass
    await cb.answer()

# ── Broadcast ────────────────────────────────────────────

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.broadcast)
    await cb.message.answer(
        "📢 <b>Broadcast Message</b>\n\nSabhi users ko bhejne wala message likho:",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(AdminStates.broadcast)
async def admin_do_broadcast(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    users = get_all_users()
    sent, failed = 0, 0
    for u in users:
        if u["is_banned"]: continue
        try:
            await bot.send_message(u["user_id"],
                f"📢 <b>Admin Message</b>\n\n{m.text}", parse_mode="HTML")
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Broadcast done!\n✅ Sent: {sent}\n❌ Failed: {failed}")

# ═══════════════════════════════════════════════════════
# ─── USER CALLBACKS ─────────────────────────────────────
# ═══════════════════════════════════════════════════════

@dp.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = cb.from_user.id
    user = get_user(uid)
    _, total_given, last_time, streak = get_stats(uid)
    next_r   = user["next_run"] if user else None
    next_str = datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"
    try:
        await cb.message.edit_text(
            f"<b>🎮 FF Like Auto-Bot</b>\n\n"
            f"⚙️ Task: {'▶️ Running' if user['task_active'] else '⏹ Stopped'}\n"
            f"🤖 Target: <code>@{user['target_bot']}</code>\n"
            f"✉️ Message: <code>{user['msg_text']}</code>\n"
            f"😴 Next run: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak}d</b> | 📦 Total: <b>{total_given}</b>\n"
            f"⏰ Last: <b>{last_str}</b>",
            parse_mode="HTML", reply_markup=main_kb(user))
    except: pass
    await cb.answer()

@dp.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    _, total_given, last_time, streak = get_stats(uid)
    next_r   = user["next_run"] if user else None
    next_str = datetime.fromisoformat(next_r).strftime("%d %b %I:%M %p IST") if next_r else "—"
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %I:%M %p IST") if last_time else "—"
    try:
        await cb.message.edit_text(
            f"<b>🔄 Status</b>\n\n"
            f"🔑 Login: {'✅' if user['session'] else '❌'}\n"
            f"⚙️ Task: {'🟢 Running' if user['task_active'] else '🔴 Stopped'}\n"
            f"🤖 Target: <code>@{user['target_bot']}</code>\n"
            f"✉️ Msg: <code>{user['msg_text']}</code>\n"
            f"⏱ Retry: <b>{user['retry_minutes']}m</b>\n"
            f"😴 Next: <b>{next_str}</b>\n\n"
            f"🔥 Streak: <b>{streak}d</b> | 📦 Total: <b>{total_given}</b>\n"
            f"⏰ Last: <b>{last_str}</b>",
            parse_mode="HTML", reply_markup=main_kb(user))
    except: pass
    await cb.answer("🔄 Refreshed!")

@dp.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    uid = cb.from_user.id
    count, total_given, last_time, streak = get_stats(uid)
    last_str = datetime.fromisoformat(last_time).strftime("%d %b %Y %I:%M %p IST") if last_time else "—"
    try:
        await cb.message.edit_text(
            f"<b>📊 Stats</b>\n\n"
            f"🔥 Streak: <b>{streak} day(s)</b>\n"
            f"📅 Total Days: <b>{count}</b>\n"
            f"❤️ Total Likes: <b>{total_given}</b>\n"
            f"⏰ Last Like: <b>{last_str}</b>",
            parse_mode="HTML", reply_markup=back_kb())
    except: pass
    await cb.answer()

@dp.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    uid  = cb.from_user.id
    rows = get_history(uid, limit=7)
    if not rows:
        try:
            await cb.message.edit_text("📋 <b>History</b>\n\nAbhi tak koi like nahi.",
                                       parse_mode="HTML", reply_markup=back_kb())
        except: pass
        await cb.answer(); return
    lines = ["<b>📋 Last 7 Likes</b>\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        if dt.tzinfo is None: dt = dt.replace(tzinfo=IST)
        lines.append(f"📅 <b>{dt.strftime('%d %b %I:%M %p')}</b>\n"
                     f"   {r['nickname'] or '—'} | {r['before']}→{r['after']} (+{r['given']})\n")
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb())
    except: pass
    await cb.answer()

@dp.callback_query(F.data == "start_task")
async def cb_start_task(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    if not user or not user["session"]:
        await cb.answer("⚠️ Pehle login karo!", show_alert=True); return
    if user["is_banned"]:
        await cb.answer("🚫 Aap banned hain!", show_alert=True); return
    upsert_user(uid, task_active=1)
    ensure_task(uid)
    try: await cb.message.edit_reply_markup(reply_markup=main_kb(get_user(uid)))
    except: pass
    await cb.answer("✅ Task started!")

@dp.callback_query(F.data == "stop_task")
async def cb_stop_task(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, task_active=0)
    try: await cb.message.edit_reply_markup(reply_markup=main_kb(get_user(uid)))
    except: pass
    await cb.answer("⏹ Task stopped!")

@dp.callback_query(F.data == "set_notify")
async def cb_set_notify(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.notify_username)
    await cb.message.answer(
        "🔔 <b>Notify Username</b>\n\nLike milne pe kise message bhejun?\n"
        "Username (without @) ya <code>none</code>:",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(SetStates.notify_username)
async def set_notify_username(m: Message, state: FSMContext):
    val = m.text.strip().lstrip("@")
    upsert_user(m.from_user.id, notify_username=None if val.lower()=="none" else val)
    await state.clear()
    msg = "🔕 Notify off." if val.lower()=="none" else f"✅ Notify: <code>@{val}</code>"
    await m.answer(msg, parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

@dp.callback_query(F.data == "set_retry")
async def cb_set_retry(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    await cb.message.answer(
        f"⏱ <b>Retry Interval</b>\nCurrent: <b>{user['retry_minutes']}m</b>\n"
        f"<i>3:50–10AM: auto 10m</i>",
        parse_mode="HTML", reply_markup=retry_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("retry_") & ~F.data.in_({"retry_custom"}))
async def cb_retry_select(cb: CallbackQuery):
    minutes = int(cb.data.split("_")[1])
    upsert_user(cb.from_user.id, retry_minutes=minutes)
    try: await cb.message.delete()
    except: pass
    await cb.message.answer(f"✅ Retry: <b>{minutes}m</b>",
                            parse_mode="HTML", reply_markup=main_kb(get_user(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "retry_custom")
async def cb_retry_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.retry_minutes)
    await cb.message.answer("✏️ Minutes (1-1440):", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(SetStates.retry_minutes)
async def set_retry_minutes(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not (1<=int(m.text.strip())<=1440):
        await m.answer("❌ 1-1440 ke beech:"); return
    upsert_user(m.from_user.id, retry_minutes=int(m.text.strip()))
    await state.clear()
    await m.answer(f"✅ Retry: <b>{m.text.strip()}m</b>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

@dp.callback_query(F.data == "cancel_retry")
async def cb_cancel_retry(cb: CallbackQuery):
    try: await cb.message.delete()
    except: pass
    await cb.answer("Cancelled")

@dp.callback_query(F.data == "set_bot")
async def cb_set_bot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.bot_username)
    await cb.message.answer(
        "🤖 Target bot username (without @):\nExample: <code>FFPlayerLikeBot</code>",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(SetStates.bot_username)
async def set_bot_username(m: Message, state: FSMContext):
    upsert_user(m.from_user.id, target_bot=m.text.strip().lstrip("@"))
    await state.clear()
    await m.answer(f"✅ Target: <code>@{m.text.strip().lstrip('@')}</code>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

@dp.callback_query(F.data == "set_msg")
async def cb_set_msg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.message_text)
    await cb.message.answer("✉️ Message:\nExample: <code>/like 1902086798</code>",
                            parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(SetStates.message_text)
async def set_message_text(m: Message, state: FSMContext):
    upsert_user(m.from_user.id, msg_text=m.text.strip())
    await state.clear()
    await m.answer(f"✅ Message: <code>{m.text.strip()}</code>",
                   parse_mode="HTML", reply_markup=main_kb(get_user(m.from_user.id)))

@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.delete()
    except: pass
    await cb.answer("Cancelled")

@dp.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, session="", task_active=0)
    try: await cb.message.edit_text("🚪 Logged out.", reply_markup=main_kb(get_user(uid)))
    except: pass
    await cb.answer()

# ─── Login Flow ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "login")
async def cb_login(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LoginStates.api_id)
    await cb.message.answer(
        "🔑 <b>Login — Step 1/5</b>\n\nApna <b>API ID</b> bhejo.\n👉 my.telegram.org",
        parse_mode="HTML", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(LoginStates.api_id)
async def login_api_id(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("❌ API ID sirf numbers:"); return
    await state.update_data(api_id=int(m.text.strip()))
    await state.set_state(LoginStates.api_hash)
    await m.answer("🔑 <b>Step 2/5</b> — <b>API Hash</b>:", parse_mode="HTML", reply_markup=cancel_kb())

@dp.message(LoginStates.api_hash)
async def login_api_hash(m: Message, state: FSMContext):
    await state.update_data(api_hash=m.text.strip())
    await state.set_state(LoginStates.phone)
    await m.answer("🔑 <b>Step 3/5</b> — Phone (with country code):\n<code>+919876543210</code>",
                   parse_mode="HTML", reply_markup=cancel_kb())

@dp.message(LoginStates.phone)
async def login_phone(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(phone=m.text.strip())
    try:
        client = TelegramClient(StringSession(), data["api_id"], data["api_hash"])
        await client.connect()
        result = await client.send_code_request(m.text.strip())
        await state.update_data(phone_code_hash=result.phone_code_hash,
                                session_str=client.session.save())
        await client.disconnect()
        await state.set_state(LoginStates.otp)
        await m.answer("🔑 <b>Step 4/5</b> — OTP:\n<code>2 3 4 5 6</code> ya <code>23456</code>",
                       parse_mode="HTML", reply_markup=cancel_kb())
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.otp)
async def login_otp(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    otp  = m.text.strip().replace(" ","")
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        try:
            await client.sign_in(phone=data["phone"], code=otp,
                                  phone_code_hash=data["phone_code_hash"])
            session_str = client.session.save()
            await client.disconnect()
            upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                        phone=data["phone"], session=session_str, task_active=0)
            await state.clear()
            await m.answer("✅ <b>Login ho gaya!</b> Ab ▶️ Start Task dabao.",
                           parse_mode="HTML", reply_markup=main_kb(get_user(uid)))
        except Exception as e:
            err = str(e)
            if "SessionPasswordNeeded" in err or "password" in err.lower():
                await state.update_data(session_str=client.session.save())
                await client.disconnect()
                await state.set_state(LoginStates.password)
                await m.answer("🔑 <b>Step 5/5</b> — 2FA Password:",
                               parse_mode="HTML", reply_markup=cancel_kb())
            else:
                await client.disconnect(); await state.clear()
                await m.answer(f"❌ OTP Error: <code>{e}</code>", parse_mode="HTML")
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.password)
async def login_password(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        await client(GetPasswordRequest())
        await client.sign_in(password=m.text.strip())
        session_str = client.session.save()
        await client.disconnect()
        upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                    phone=data["phone"], session=session_str,
                    password_2fa=m.text.strip(), task_active=0)
        await state.clear()
        await m.answer("✅ <b>Login ho gaya!</b> Ab ▶️ Start Task dabao.",
                       parse_mode="HTML", reply_markup=main_kb(get_user(uid)))
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Password galat!\n<code>{e}</code>\n\nDobara /start se try karo.",
                       parse_mode="HTML")

# ─── Startup ─────────────────────────────────────────────────────────────────

async def resume_tasks():
    with db_conn() as c:
        rows = c.execute("SELECT user_id FROM users WHERE task_active=1 AND is_banned=0").fetchall()
    for row in rows:
        log.info(f"Resuming {row['user_id']}")
        ensure_task(row["user_id"])

async def main():
    await resume_tasks()
    asyncio.create_task(health_check_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
