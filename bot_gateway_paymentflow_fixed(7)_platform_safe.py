import os
import asyncio
import sqlite3
import logging
import json
import html
import urllib.parse
import requests
from decimal import Decimal, InvalidOperation

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8106689891:AAFEMpemYQ9lSkgywaQbAcDbFLgVpKMWqws")

# YOUR TELEGRAM USER ID
ADMIN_IDS = { 8926100324
    
}

DB_FILE = "reward_bot.db"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row



# ============================================================
# DATABASE
# ============================================================

def init_db():

    db.executescript("""

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        name TEXT DEFAULT '',
        balance REAL DEFAULT 0,
        upi TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        link TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS task_claims (
        task_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(task_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS task_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        request_type TEXT NOT NULL,
        submission TEXT DEFAULT '',
        screenshot_file_id TEXT DEFAULT '',
        status TEXT DEFAULT 'PENDING',
        reward REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        processed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        per_account INTEGER DEFAULT 1,
        total_limit INTEGER DEFAULT 1,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS gift_claims (
        gift_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(gift_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        method TEXT NOT NULL,
        payment TEXT NOT NULL,
        status TEXT DEFAULT 'PENDING',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        processed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender INTEGER NOT NULL,
        receiver INTEGER NOT NULL,
        amount REAL NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    """)

    # Safe migration for existing SQLite databases
    cols = [r["name"] for r in db.execute("PRAGMA table_info(gifts)").fetchall()]
    if "total_limit" not in cols:
        db.execute("ALTER TABLE gifts ADD COLUMN total_limit INTEGER DEFAULT 1")

    withdrawal_cols = [r["name"] for r in db.execute("PRAGMA table_info(withdrawals)").fetchall()]
    if "payout_chat_id" not in withdrawal_cols:
        db.execute("ALTER TABLE withdrawals ADD COLUMN payout_chat_id TEXT DEFAULT ''")
    if "payout_message_id" not in withdrawal_cols:
        db.execute("ALTER TABLE withdrawals ADD COLUMN payout_message_id INTEGER")
    if "gateway_name" not in withdrawal_cols:
        db.execute("ALTER TABLE withdrawals ADD COLUMN gateway_name TEXT DEFAULT ''")
    db.commit()


# ============================================================
# HELPERS
# ============================================================

def get_extra_admin_ids():
    try:
        values = json.loads(get_setting("extra_admins") or "[]")
        return {int(x) for x in values}
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def is_owner(user_id):
    return user_id in ADMIN_IDS


def is_admin(user_id):
    return user_id in ADMIN_IDS or user_id in get_extra_admin_ids()


def local_timestamp():
    # India Standard Time, independent of host server timezone.
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %H:%M:%S")


def money(value):
    return f"₹{float(value):.2f}"


def parse_amount(value):

    try:
        x = Decimal(
            str(value)
            .strip()
            .replace(",", "")
            .replace("₹", "")
        )

        if x <= 0:
            return None

        return float(
            x.quantize(
                Decimal("0.01")
            )
        )

    except (InvalidOperation, ValueError):
        return None


def save_user(user):

    db.execute("""
        INSERT INTO users(
            user_id,
            username,
            name
        )
        VALUES(?,?,?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            name=excluded.name
    """, (
        user.id,
        user.username or "",
        user.full_name or ""
    ))

    db.commit()


def get_user(user_id):

    return db.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()


def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    db.commit()


def clear_state(context):

    context.user_data.pop("state", None)
    context.user_data.pop("admin_state", None)

    for key in [
        "task_id",
        "gift_id",
        "gift_code",
        "gift_name",
        "gift_amount",
        "gift_limit",
        "task_name",
        "task_amount",
        "task_link",
        "receiver",
        "balance_user",
        "request_type"
    ]:
        context.user_data.pop(key, None)


async def check_forced_sub(update, context):
    """Checks if forced subscription is active and if user joined up to 4 channels."""
    user_id = update.effective_user.id
    if is_admin(user_id):
        return True

    channels_raw = get_setting("required_channels")
    if not channels_raw:
        return True

    channels = [ch.strip() for ch in channels_raw.split(",") if ch.strip()]
    if not channels:
        return True

    for ch_link in channels:
        try:
            clean_ch = ch_link
            if "t.me/" in clean_ch:
                clean_ch = "@" + clean_ch.split("t.me/")[-1].split("/")[0]
            
            member = await context.bot.get_chat_member(chat_id=clean_ch, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception as e:
            logging.error(f"Forced sub check error for {ch_link}: {e}")
            return False

    return True


# ============================================================
# USER MAIN MENU
# ============================================================

def main_menu(user_id):
    # Defensive menu rendering: invalid saved layouts or unsupported KeyboardButton
    # style arguments must never prevent /start from showing the user menu.
    try:
        layout=json.loads(get_setting("menu_layout") or json.dumps(MENU_DEFAULT))
        if not isinstance(layout, list) or not layout: layout=MENU_DEFAULT
    except Exception: layout=MENU_DEFAULT
    try: labels=json.loads(get_setting("menu_labels") or "{}")
    except Exception: labels={}
    try: colors=json.loads(get_setting("menu_colors") or "{}")
    except Exception: colors={}
    buttons=[]
    style_map = {"blue": "primary", "green": "success", "red": "danger"}
    for row in layout:
        if not isinstance(row, list): continue
        rendered=[]
        for key in row:
            label=labels.get(key, MENU_LABELS.get(key,key))
            color=colors.get(key,"none")
            # Use Telegram's reply-keyboard button style field (no colour-dot prefix).
            style = style_map.get(color)
            if style:
                rendered.append(KeyboardButton(text=label, api_kwargs={"style": style}))
            else:
                rendered.append(KeyboardButton(text=label))
        if rendered: buttons.append(rendered)
    if not buttons:
        buttons = MENU_DEFAULT
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# ============================================================
# BACK BUTTONS
# ============================================================

def back_main():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_main"
            )
        ]
    ])


def back_tasks():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_tasks"
            )
        ]
    ])


def back_admin():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_admin"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):

    save_user(update.effective_user)
    clear_state(context)
    if get_setting("maintenance") == "1" and not is_admin(update.effective_user.id):
        until = get_setting("maintenance_until") or "until further notice"
        await update.message.reply_text(f"🛠 Currently bot maintenance.\nExpected end: {until}")
        return

    if not await check_forced_sub(update, context):
        channels_raw = get_setting("required_channels")
        channels = [ch.strip() for ch in channels_raw.split(",") if ch.strip()]
        
        keyboard = []
        for i, ch_link in enumerate(channels, 1):
            keyboard.append([InlineKeyboardButton(f"📢 Join Channel {i}", url=ch_link)])
        
        keyboard.append([InlineKeyboardButton("✅ Claim / Check Join", callback_data="check_join")])

        await update.message.reply_text(
            "📢 **CHANNEL SUBSCRIPTION REQUIRED**\n\n"
            "You must join all official channels to use this bot!\n\n"
            "1. Click the buttons below to join each channel.\n"
            "2. Come back and click **Claim / Check Join**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "🎉 Welcome to ERAN TASK BOT!\n\nChoose an option:",
        reply_markup=main_menu(
            update.effective_user.id
        )
    )


async def check_join_callback(update, context):
    query = update.callback_query
    await query.answer()

    if await check_forced_sub(update, context):
        await query.message.edit_text(
            "🎉 Thank you for joining! Choose an option from the menu below:"
        )
        await context.bot.send_message(
            query.from_user.id,
            "🏠 Main Menu:",
            reply_markup=main_menu(query.from_user.id)
        )
    else:
        await query.answer("❌ You have not joined all required channels yet!", show_alert=True)


async def my_id(update, context):

    save_user(update.effective_user)

    await update.message.reply_text(
        f"🆔 Your Telegram User ID:\n\n"
        f"{update.effective_user.id}",
        reply_markup=back_main()
    )


# ============================================================
# BOT TASK LIST
# ============================================================

async def show_tasks_message(update, context):

    if not await check_forced_sub(update, context):
        return await start(update, context)

    save_user(update.effective_user)
    clear_state(context)

    tasks = db.execute("""
        SELECT *
        FROM tasks
        WHERE active=1
          AND id NOT IN (
              SELECT task_id FROM task_requests
              WHERE user_id=? AND status='APPROVED'
          )
        ORDER BY id DESC
    """, (update.effective_user.id,)).fetchall()

    if not tasks:

        await update.message.reply_text(
            "📋 AVAILABLE TASKS\n\n"
            "No tasks available right now.",
            reply_markup=back_main()
        )

        return

    keyboard = []

    for task in tasks:

        keyboard.append([
            InlineKeyboardButton(
                f"{task['name']} ⚡ ({money(task['amount'])})",
                callback_data=f"task:{task['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ BACK",
            callback_data="back_main"
        )
    ])

    await update.message.reply_text(
        "📋 AVAILABLE TASKS\n\n"
        "Select a task:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# TASK DETAILS
# ============================================================

async def task_details(update, context):

    query = update.callback_query
    await query.answer()

    task_id = int(
        query.data.split(":")[1]
    )

    task = db.execute("""
        SELECT *
        FROM tasks
        WHERE id=? AND active=1
    """, (task_id,)).fetchone()

    if not task:

        await query.message.reply_text(
            "❌ Task no longer available.",
            reply_markup=back_tasks()
        )

        return

    context.user_data["task_id"] = task_id

    approved = db.execute("""
        SELECT 1
        FROM task_requests
        WHERE task_id=?
        AND user_id=?
        AND status='APPROVED'
        LIMIT 1
    """, (
        task_id,
        query.from_user.id
    )).fetchone()

    pending = db.execute("""
        SELECT 1
        FROM task_requests
        WHERE task_id=?
        AND user_id=?
        AND status='PENDING'
        LIMIT 1
    """, (
        task_id,
        query.from_user.id
    )).fetchone()

    if approved:

        status_button = InlineKeyboardButton(
            "✅ TASK COMPLETED",
            callback_data="nothing"
        )

    elif pending:

        status_button = InlineKeyboardButton(
            "⏳ SUBMISSION PENDING",
            callback_data="nothing"
        )

    else:

        status_button = None

    keyboard = [

        [
            InlineKeyboardButton(
                "🔗 TASK LINK",
                url=task["link"]
            )
        ],

        [
            InlineKeyboardButton(
                "📸 UPLOAD SCREENSHOT",
                callback_data=f"upload:{task_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "🔢 SEND REFER LINK / NUMB",
                callback_data=f"refer:{task_id}"
            )
        ]

    ]

    if status_button:
        keyboard.append([
            status_button
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ BACK",
            callback_data="back_tasks"
        )
    ])

    await query.message.reply_text(

        f"📋 TASK DETAILS\n\n"
        f"📝 TASK NAME: {task['name']}\n"
        f"💰 REWARD: {money(task['amount'])}\n"
        f"🔗 LINK:\n{task['link']}\n\n"
        f"⚠️ Complete the task and submit proof.\n"
        f"Admin will review your submission.",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# ============================================================
# TASK SCREENSHOT
# ============================================================

async def upload_task(update, context):

    query = update.callback_query
    await query.answer()

    task_id = int(
        query.data.split(":")[1]
    )

    context.user_data.clear()

    context.user_data["state"] = "task_screenshot"
    context.user_data["task_id"] = task_id

    await query.message.reply_text(

        "📸 UPLOAD SCREENSHOT\n\n"
        "Complete the task first.\n"
        "Then send the screenshot here.\n\n"
        "Admin will review it before granting the reward.",

        reply_markup=back_tasks()
    )


# ============================================================
# TASK REFER LINK / NUMBER
# ============================================================

async def refer_task(update, context):

    query = update.callback_query
    await query.answer()

    task_id = int(
        query.data.split(":")[1]
    )

    context.user_data.clear()

    context.user_data["state"] = "task_refer"
    context.user_data["task_id"] = task_id

    await query.message.reply_text(

        "🔢 SEND REFER LINK / NUMBER\n\n"
        "Send your referral link or referral number.",

        reply_markup=back_tasks()
    )


# ============================================================
# TASK SUBMISSION
# ============================================================

async def submit_task_request(
    update,
    context,
    request_type,
    submission="",
    screenshot=""
):

    task_id = context.user_data.get(
        "task_id"
    )

    if not task_id:

        await update.message.reply_text(
            "❌ Task session expired.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        clear_state(context)

        return

    task = db.execute("""
        SELECT *
        FROM tasks
        WHERE id=? AND active=1
    """, (task_id,)).fetchone()

    if not task:

        await update.message.reply_text(
            "❌ Task is no longer available."
        )

        clear_state(context)

        return

    # Atomically create a pending submission to avoid duplicate/racing requests.
    try:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT 1 FROM task_requests WHERE task_id=? AND user_id=? AND status='PENDING'",
                             (task_id, update.effective_user.id)).fetchone()
        approved = db.execute("SELECT 1 FROM task_requests WHERE task_id=? AND user_id=? AND status='APPROVED'",
                              (task_id, update.effective_user.id)).fetchone()
        if pending or approved:
            db.rollback()
            await update.message.reply_text(
                "⏳ You already have a pending submission or an approved completion for this task.",
                reply_markup=main_menu(update.effective_user.id))
            clear_state(context)
            return
        db.execute("INSERT INTO task_requests(task_id,user_id,request_type,submission,screenshot_file_id,reward) VALUES(?,?,?,?,?,?)",
                   (task_id, update.effective_user.id, request_type, submission, screenshot, task["amount"]))
        request_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    except Exception:
        db.rollback()
        logging.exception("Task submission insert failed")
        await update.message.reply_text("❌ Submission could not be saved. Please try again.", reply_markup=back_tasks())
        return

    for admin_id in (ADMIN_IDS | get_extra_admin_ids()):

        try:

            user = get_user(
                update.effective_user.id
            )

            admin_text = (
                f"📥 NEW TASK SUBMISSION\n\n"
                f"Request ID: <code>{request_id}</code>\n"
                f"Task ID: <code>{task_id}</code>\n"
                f"Task: {html.escape(str(task['name']))}\n"
                f"Reward: {money(task['amount'])}\n\n"
                f"👤 User ID: <code>{user['user_id']}</code>\n"
                f"Name: {html.escape(str(user['name']))}\n"
                f"Username: @{html.escape(str(user['username'] or '-'))}\n\n"
                f"Type: {html.escape(str(request_type))}\n"
            )

            if submission:
                admin_text += (
                    f"\n📨 Submission:\n"
                    f"{html.escape(str(submission))}\n"
                )

            if screenshot:
                admin_text += "\n📸 Screenshot attached."

            admin_text += "\nChoose Approve or Reject."

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ APPROVE",
                        callback_data=f"taskapprove:{request_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ REJECT",
                        callback_data=f"taskreject:{request_id}"
                    )
                ]
            ])

            await context.bot.send_message(
                admin_id,
                admin_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

            if screenshot:

                await context.bot.send_photo(
                    admin_id,
                    screenshot,
                    caption=(
                        f"📸 TASK PROOF\n"
                        f"Request #{request_id}\n"
                        f"User ID: {user['user_id']}"
                    )
                )

        except Exception as e:

            logging.error(
                "Admin notification error: %s",
                e
            )

    clear_state(context)

    await update.message.reply_text(

        f"✅ SUBMISSION SENT\n\n"
        f"Request: #{request_id}\n"
        f"Task: {task['name']}\n"
        f"Reward: {money(task['amount'])}\n\n"
        f"⏳ Status: PENDING\n\n"
        f"Admin will review your submission.\n"
        f"Reward is added only after approval.",

        reply_markup=main_menu(
            update.effective_user.id
        )
    )


# ============================================================
# GIFT MENU
# ============================================================

async def gift_menu(update, context):

    if not await check_forced_sub(update, context):
        return await start(update, context)

    clear_state(context)
    context.user_data["state"] = "gift"

    await update.message.reply_text(

        "🎁 GIFT CODE\n\n"
        "Enter your gift code:",

        reply_markup=back_main()
    )


async def process_gift(update, context):

    code = update.message.text.strip()

    gift = db.execute("""
        SELECT *
        FROM gifts
        WHERE code=?
        AND active=1
    """, (code,)).fetchone()

    if not gift:

        await update.message.reply_text(
            "❌ Invalid gift code.",
            reply_markup=back_main()
        )

        return

    total_used = db.execute("SELECT COUNT(*) FROM gift_claims WHERE gift_id=?", (gift["id"],)).fetchone()[0]
    if total_used >= gift["total_limit"]:
        await update.message.reply_text("⏳ Try next time. This gift code has reached its total use limit.", reply_markup=back_main())
        return

    used = db.execute("""
        SELECT COUNT(*)
        FROM gift_claims
        WHERE gift_id=?
        AND user_id=?
    """, (
        gift["id"],
        update.effective_user.id
    )).fetchone()[0]

    if used >= gift["per_account"]:

        await update.message.reply_text(
            "⚠️ This gift code has reached your account limit.",
            reply_markup=back_main()
        )

        return

    db.execute("""
        INSERT INTO gift_claims(
            gift_id,
            user_id
        )
        VALUES(?,?)
    """, (
        gift["id"],
        update.effective_user.id
    ))

    db.execute("""
        UPDATE users
        SET balance=balance+?
        WHERE user_id=?
    """, (
        gift["amount"],
        update.effective_user.id
    ))

    db.commit()

    clear_state(context)

    await update.message.reply_text(

        f"🎉 GIFT SUCCESSFUL!\n\n"
        f"🎁 {gift['name']}\n"
        f"💰 Added: {money(gift['amount'])}",

        reply_markup=main_menu(
            update.effective_user.id
        )
    )


# ============================================================
# MY BALANCE
# ============================================================

async def my_balance(update, context):
    """Wallet overview; history and live fund are available inside this screen."""
    if not await check_forced_sub(update, context):
        return await start(update, context)

    clear_state(context)
    user = get_user(update.effective_user.id)
    fund = float(get_setting("bot_fund") or 0)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 EARNING HISTORY", callback_data="wallet_earning"),
            InlineKeyboardButton("🧾 WITHDRAWAL HISTORY", callback_data="wallet_withdrawal"),
        ],
        [InlineKeyboardButton("💰 BOT FUND / LIVE FUND", callback_data="wallet_fund")],
        [InlineKeyboardButton("🔄 REFRESH", callback_data="wallet_refresh")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
    ])
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━\n"
        "💳 WALLET OVERVIEW\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔵 WALLET ID → <a href=\"tg://user?id={user['user_id']}\">{user['user_id']}</a>\n"
        f"🧾 BALANCE → {money(user['balance'])}\n"
        f"👤 TOTAL USER → {db.execute('SELECT COUNT(*) FROM users').fetchone()[0]}\n\n"
        "Built with security you can trust.\n"
        "Support that responds promptly.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ============================================================
# USER HISTORY
# ============================================================

async def earning_history(update, context):
    rows = db.execute("""
        SELECT r.*, t.name AS task_name FROM task_requests r
        LEFT JOIN tasks t ON t.id=r.task_id
        WHERE r.user_id=? AND r.status='APPROVED'
        ORDER BY r.id DESC LIMIT 30
    """, (update.effective_user.id,)).fetchall()
    if not rows:
        text = "📜 EARNING HISTORY\\n\\nNo approved earnings yet."
    else:
        text = "📜 EARNING HISTORY (approved tasks)\\n\\n"
        for r in rows:
            text += f"✅ {r['task_name'] or 'Task'} — {money(r['reward'])}\\n"
    await update.message.reply_text(text, reply_markup=back_main())


async def user_withdrawal_history(update, context):
    rows = db.execute("""
        SELECT amount, status, created_at FROM withdrawals
        WHERE user_id=? ORDER BY id DESC LIMIT 30
    """, (update.effective_user.id,)).fetchall()
    if not rows:
        text = "🧾 WITHDRAWAL HISTORY\\n\\nNo withdrawal requests yet."
    else:
        text = "🧾 WITHDRAWAL HISTORY\\n\\n"
        for r in rows:
            text += f"{r['created_at']} | {money(r['amount'])} | {r['status']}\\n"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ WALLET", callback_data="wallet_refresh")],
        [InlineKeyboardButton("🏠 MAIN MENU", callback_data="back_main")],
    ]))


# ============================================================
# PAYOUT METHOD
# ============================================================

async def set_upi(update, context):
    query = update.callback_query
    if get_setting("upi_enabled") == "0":
        await query.answer("UPI is disabled.", show_alert=True)
        return
    await query.answer()
    context.user_data["state"] = "upi"
    await query.message.reply_text("💳 SET UPI\n\nSend your UPI ID:", reply_markup=back_main())

async def save_upi(update, context):
    value = (update.message.text or "").strip()
    if "@" not in value or " " in value or len(value) < 5:
        await update.message.reply_text("❌ Enter a valid UPI ID and try again:", reply_markup=back_main())
        return
    db.execute("UPDATE users SET upi=? WHERE user_id=?", (value, update.effective_user.id))
    db.commit()
    context.user_data.pop("state", None)
    await update.message.reply_text(f"✅ UPI saved successfully.\n\nUPI: {value}", reply_markup=main_menu(update.effective_user.id))


async def payout_method(update, context):
    if not await check_forced_sub(update, context):
        return await start(update, context)
    clear_state(context)
    user=get_user(update.effective_user.id)
    saved=json.loads(get_setting(f"gateway_ids_{update.effective_user.id}") or "{}")
    lines=["💳 PAYMENT METHOD",""]
    buttons=[]
    if get_setting("upi_enabled")!="0":
        lines.append(f"UPI = {user['upi'] or 'Not set'}")
        buttons.append([InlineKeyboardButton("SET UPI",callback_data="setupi")])
    for name in get_gateways():
        lines.append(f"{name} = {saved.get(name) or 'Not set'}")
        buttons.append([InlineKeyboardButton(f"SET {name}",callback_data=f"set_gateway_id:{name}")])
    lines += ["","Set or update your UPI or gateway payment ID here. Saved details will be used during withdrawal."]
    buttons.append([InlineKeyboardButton("⬅️ BACK",callback_data="back_main")])
    await update.message.reply_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons))




# ============================================================
# QUICK PAY
# ============================================================

async def quick_pay(update, context):

    if not await check_forced_sub(update, context):
        return await start(update, context)

    clear_state(context)
    context.user_data["state"] = "send_user"

    await update.message.reply_text(

        "⚡ QUICK PAY\n\n"
        "Enter receiver User ID:",

        reply_markup=back_main()
    )


async def quick_pay_user(update, context):

    try:

        receiver = int(
            update.message.text.strip()
        )

    except:

        await update.message.reply_text(
            "❌ User ID must be a number.",
            reply_markup=back_main()
        )

        return

    if receiver == update.effective_user.id:

        await update.message.reply_text(
            "❌ You cannot send money to yourself.",
            reply_markup=back_main()
        )

        return

    user = get_user(receiver)

    if not user:

        await update.message.reply_text(
            "❌ User not found.\n"
            "Receiver must open the bot first.",
            reply_markup=back_main()
        )

        return

    context.user_data["receiver"] = receiver
    context.user_data["state"] = "send_amount"

    await update.message.reply_text(

        f"👤 Receiver:\n"
        f"{user['name']}\n"
        f"ID: {receiver}\n\n"
        f"💰 Enter amount:",

        reply_markup=back_main()
    )


async def quick_pay_amount(update, context):

    value = parse_amount(
        update.message.text
    )

    if value is None:

        await update.message.reply_text(
            "❌ Enter a valid amount.",
            reply_markup=back_main()
        )

        return

    sender = get_user(
        update.effective_user.id
    )

    receiver = context.user_data.get(
        "receiver"
    )

    if not sender or sender["balance"] < value:

        await update.message.reply_text(
            "❌ Insufficient balance.",
            reply_markup=back_main()
        )

        return

    db.execute("""
        UPDATE users
        SET balance=balance-?
        WHERE user_id=?
        AND balance>=?
    """, (
        value,
        sender["user_id"],
        value
    ))

    db.execute("""
        UPDATE users
        SET balance=balance+?
        WHERE user_id=?
    """, (
        value,
        receiver
    ))

    db.execute("""
        INSERT INTO transfers(
            sender,
            receiver,
            amount
        )
        VALUES(?,?,?)
    """, (
        sender["user_id"],
        receiver,
        value
    ))

    db.commit()

    clear_state(context)

    await update.message.reply_text(

        f"✅ PAYMENT SENT\n\n"
        f"Amount: {money(value)}\n"
        f"Receiver ID: {receiver}",

        reply_markup=main_menu(
            update.effective_user.id
        )
    )

    try:

        await context.bot.send_message(
            receiver,
            f"💰 PAYMENT RECEIVED\n\n"
            f"Amount: {money(value)}\n"
            f"From User ID: {sender['user_id']}"
        )

    except:
        pass


# ============================================================
# WITHDRAW
# ============================================================

async def withdraw_menu(update, context):
    if not await check_forced_sub(update, context): return await start(update, context)
    clear_state(context)
    fund=float(get_setting("bot_fund") or 0)
    if fund<=0:
        set_setting("withdraw_enabled","0"); await update.message.reply_text("🚫 Withdrawals OFF: bot fund is ₹0.",reply_markup=back_main()); return
    if get_setting("withdraw_enabled")=="0" or get_setting("maintenance")=="1":
        await update.message.reply_text("🚫 Withdrawals unavailable.",reply_markup=back_main()); return
    rows=[]
    if get_setting("upi_enabled")!="0": rows.append([InlineKeyboardButton("UPI",callback_data="withdraw_method:UPI")])
    for name in get_gateways(): rows.append([InlineKeyboardButton(name,callback_data=f"withdraw_method:{name}")])
    rows.append([InlineKeyboardButton("⬅️ BACK",callback_data="back_main")])
    await update.message.reply_text("🚀 WITHDRAW\\n\\nSelect payment method:",reply_markup=InlineKeyboardMarkup(rows))

def mask_payment_address(value):
    value = str(value or "")
    if "@" in value:
        local, domain = value.split("@", 1)
        visible = local[:3] if len(local) > 3 else local[:1]
        return visible + "***@" + domain
    if len(value) <= 5:
        return value[:1] + "***"
    return value[:2] + "*****" + value[-3:]

async def create_withdrawal(update, context):
    value=parse_amount(update.message.text)
    if value is None:
        await update.message.reply_text("❌ Enter a valid amount.",reply_markup=back_main()); return
    method=context.user_data.get("withdraw_method"); payment=context.user_data.get("withdraw_payment")
    user=get_user(update.effective_user.id); fund=float(get_setting("bot_fund") or 0)
    if not user or value>user["balance"] or value>fund:
        await update.message.reply_text("❌ Insufficient balance or bot fund.",reply_markup=back_main()); return
    if get_setting("withdraw_enabled")=="0": await update.message.reply_text("🚫 Withdrawals off.",reply_markup=back_main()); return
    if method=="UPI" and get_setting("upi_enabled")=="0": await update.message.reply_text("UPI unavailable.",reply_markup=back_main()); return
    if method!="UPI" and method not in get_gateways(): clear_state(context); await update.message.reply_text("Gateway unavailable.",reply_markup=back_main()); return
    context.user_data["withdraw_amount"]=value
    context.user_data["state"]="withdraw_confirm"
    markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm ✅",callback_data="withdraw_confirm"),InlineKeyboardButton("Cancel ❌",callback_data="withdraw_cancel")]])
    masked_payment = mask_payment_address(payment)
    confirmation_text=(
        "<b>Withdrawal Confirmation</b>\n\n"
        f"<blockquote>Amount: {money(value)}\n"
        f"You receive: {money(value)} (Tax: ₹0.00)\n\n"
        f"Wallet: {html.escape(masked_payment)}</blockquote>\n\n"
        "Confirm your transaction by clicking Confirm"
    )
    await update.message.reply_text(confirmation_text,reply_markup=markup,parse_mode="HTML")

async def submit_confirmed_withdrawal(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    value = context.user_data.get("withdraw_amount")
    method = context.user_data.get("withdraw_method")
    payment = context.user_data.get("withdraw_payment")
    if not value or not method or not payment:
        await query.answer("Session expired. Start again.", show_alert=True); return
    user = get_user(user_id); fund = float(get_setting("bot_fund") or 0)
    if not user or value > user["balance"] or value > fund or get_setting("withdraw_enabled") == "0":
        await query.answer("Balance/fund unavailable. Request not submitted.", show_alert=True); return
    if method == "UPI" and get_setting("upi_enabled") == "0":
        await query.answer("UPI is disabled.", show_alert=True); return
    if method != "UPI" and method not in get_gateways():
        await query.answer("Gateway unavailable.", show_alert=True); return
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (value,user_id,value))
        if db.execute("SELECT changes()").fetchone()[0] == 0: raise ValueError("balance")
        db.execute("INSERT INTO withdrawals(user_id,amount,method,payment,status,created_at,gateway_name) VALUES(?,?,?,?,?,?,?)",(user_id,value,method,payment,"PENDING",local_timestamp(),method if method!="UPI" else ""))
        rid=db.execute("SELECT last_insert_rowid()").fetchone()[0]
        remaining=max(0.0,fund-value); set_setting("bot_fund",f"{remaining:.2f}")
        if remaining<=0: set_setting("withdraw_enabled","0")
        db.commit()
    except Exception:
        db.rollback(); await query.message.reply_text("❌ Could not create withdrawal.",reply_markup=back_main()); return

    gateway_response_text = ""
    final_status = "PENDING"
    if method != "UPI":
        endpoint=get_gateways().get(method)
        try:
            if not endpoint: raise ValueError("Gateway endpoint not configured")
            url=endpoint.format(number=urllib.parse.quote(str(payment),safe=""),amount=urllib.parse.quote(f"{float(value):.2f}",safe=""),comment=urllib.parse.quote(f"Withdrawal #{rid}",safe=""))
            def _get():
                response = requests.get(url, headers={"User-Agent":"TelegramBot/1.0"}, timeout=20)
                response.raise_for_status()
                return response.text
            gateway_response_text=await asyncio.to_thread(_get)
            parsed=json.loads(gateway_response_text)
            response_status=str(parsed.get("status",parsed.get("success",parsed.get("result","")))).lower()
            if response_status not in ("success","successful","ok","true","1"):
                raise RuntimeError("Gateway did not confirm success")
            final_status="APPROVED"
            db.execute("UPDATE withdrawals SET status='APPROVED',processed_at=CURRENT_TIMESTAMP WHERE id=? AND status='PENDING'",(rid,)); db.commit()
        except Exception as exc:
            logging.exception("Automatic gateway withdrawal failed")
            db.execute("UPDATE withdrawals SET status='REJECTED',processed_at=CURRENT_TIMESTAMP WHERE id=?",(rid,))
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(value,user_id))
            restored=float(get_setting("bot_fund") or 0)+float(value); set_setting("bot_fund",f"{restored:.2f}"); set_setting("withdraw_enabled","1")
            db.commit()
            final_status="FAILED"
            gateway_response_text=str(exc)

    clear_state(context)
    updated=get_user(user_id)
    if method == "UPI":
        user_text=(f"✅ ᴡɪᴛʜᴅʀᴀᴡᴀʟ ʀᴇǫᴜᴇꜱᴛ ꜱᴜʙᴍɪᴛᴛᴇᴅ!\n\n"
            f"💰 ᴀᴍᴏᴜɴᴛ: {money(value)}\n"
            f"⚡ UPI ID: {payment}\n"
            f"🆔 ɪᴅ: {user_id}\n\n"
            "⏳ ᴡᴀɪᴛɪɴɢ ꜰᴏʀ ᴀᴅᴍɪɴ ᴀᴘᴘʀᴏᴠᴀʟ...\n"
            f"💵 ɴᴇᴡ ʙᴀʟᴀɴᴄᴇ: {money(updated['balance'])}")
    elif final_status == "APPROVED":
        user_text=f"✅ Automatic payout successful\n\nAmount: {money(value)}\nMethod: {method}\nBalance: {money(updated['balance'])}\n\nGateway response: {gateway_response_text[:500]}"
    else:
        user_text=f"❌ Automatic payout failed; amount refunded.\n\nAmount: {money(value)}\nMethod: {method}\nBalance: {money(updated['balance'])}\n\nResponse: {gateway_response_text[:500]}"
    try: await query.message.edit_text(user_text)
    except Exception: await context.bot.send_message(user_id,user_text)

    # UPI is manual; gateways are automatic. Admin DMs retain full payment addresses.
    if method == "UPI":
        request_text=(
            "✅ <b>New Withdrawal request</b> ✅\n\n"
            f"🟢 User : <code>{user_id}</code>\n"
            f"🤘 Remaining Balance :- {money(updated['balance'])}\n\n"
            f"🚀 Amount : {money(value)} INR (-)\n"
            f"⛔ Address : <code>{html.escape(str(payment))}</code>"
        )
        markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ APPROVE",callback_data=f"approve:{rid}"),InlineKeyboardButton("❌ REJECT",callback_data=f"reject:{rid}")]])
        for admin in (ADMIN_IDS|get_extra_admin_ids()):
            try: await context.bot.send_message(admin,request_text,parse_mode="HTML",reply_markup=markup)
            except Exception: logging.exception("Admin UPI payout notification failed")
    else:
        try:
            parsed_response = json.loads(gateway_response_text)
        except Exception:
            parsed_response = gateway_response_text
        response_display = html.escape(json.dumps(parsed_response, ensure_ascii=False) if isinstance(parsed_response, (dict,list)) else str(parsed_response)[:700])
        if final_status == "APPROVED":
            request_text=(
                "✅ <b>New Withdrawal Processed</b> ✅\n\n"
                f"🟢 User : <code>{user_id}</code>\n\n"
                f"🚀 Amount : {money(value)} INR (-)\n"
                f"⛔ Address : <code>{html.escape(str(payment))}</code>\n\n"
                f"💡 Bot: @{context.bot.username or 'bot'}\n\n"
                f"⚠️ Gateway Response: <code>{response_display}</code>"
            )
        else:
            request_text=(
                "❌ <b>New Withdrawal Failed &amp; Refunded</b> ❌\n\n"
                f"🟢 User : <code>{user_id}</code>\n\n"
                f"🚀 Amount : {money(value)} INR (-)\n"
                f"⛔ Address : <code>{html.escape(str(payment))}</code>\n\n"
                f"💡 Bot: @{context.bot.username or 'bot'}\n\n"
                f"⚠️ Gateway Response: <code>{response_display}</code>"
            )
        markup=None

    channel=get_setting("payout_channel") or ""
    if channel:
        try:
            msg=await context.bot.send_message(channel,request_text,parse_mode="HTML",reply_markup=markup)
            db.execute("UPDATE withdrawals SET payout_chat_id=?,payout_message_id=? WHERE id=?",(str(msg.chat_id),msg.message_id,rid)); db.commit()
        except Exception: logging.exception("Payout channel post failed")


# ADMIN MENU
# ============================================================

MENU_DEFAULT = [["BOT TASK", "MY BALANCE"], ["GIFT CODE", "QUICK PAY", "WITHDRAW"], ["PAYOUT METHOD", "OFFICIAL SUPPORT"]]
MENU_LABELS = {"BOT TASK":"📋 BOT TASK", "MY BALANCE":"💸 MY BALANCE", "GIFT CODE":"🎁 GIFT CODE", "QUICK PAY":"⚡ QUICK PAY", "WITHDRAW":"🚀 WITHDRAW", "PAYOUT METHOD":"💳 PAYOUT METHOD", "OFFICIAL SUPPORT":"🆘 Official Support"}

def menu_editor_keyboard():
    try:
        layout = json.loads(get_setting("menu_layout") or json.dumps(MENU_DEFAULT))
    except Exception:
        layout = MENU_DEFAULT
    try:
        labels = json.loads(get_setting("menu_labels") or "{}")
    except Exception:
        labels = {}
    try:
        colors = json.loads(get_setting("menu_colors") or "{}")
    except Exception:
        colors = {}

    rows = []
    order = 1
    for row_index, row in enumerate(layout):
        for key in row:
            label = labels.get(key, MENU_LABELS.get(key, key))
            color = colors.get(key, "none")
            marker = {"blue": "🔵", "green": "🟢", "red": "🔴"}.get(color, "⚪")
            rows.append([InlineKeyboardButton(f"{order}. {marker} {label}", callback_data=f"menu_pick:{key}")])
            order += 1
    rows.append([InlineKeyboardButton("💾 SAVE & APPLY TO USERS", callback_data="menu_save")])
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")])
    return InlineKeyboardMarkup(rows)


def menu_editor_action_keyboard(key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename selected button", callback_data="menu_rename")],
        [InlineKeyboardButton("🔵 Blue", callback_data=f"menu_color:{key}:blue"), InlineKeyboardButton("🟢 Green", callback_data=f"menu_color:{key}:green")],
        [InlineKeyboardButton("🔴 Red", callback_data=f"menu_color:{key}:red"), InlineKeyboardButton("⚪ Default", callback_data=f"menu_color:{key}:none")],
        [InlineKeyboardButton("⬆️ Up", callback_data=f"menu_move:{key}:up"), InlineKeyboardButton("⬇️ Down", callback_data=f"menu_move:{key}:down")],
        [InlineKeyboardButton("⬅️ Left", callback_data=f"menu_move:{key}:left"), InlineKeyboardButton("➡️ Right", callback_data=f"menu_move:{key}:right")],
        [InlineKeyboardButton("↩️ Select another button", callback_data="admin_menu_editor")],
        [InlineKeyboardButton("💾 SAVE & APPLY TO ALL USERS", callback_data="menu_save")],
    ])

def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ ADD BOT TASK",
                callback_data="admin_add_task"
            ),
            InlineKeyboardButton(
                "✏️ EDIT TASK",
                callback_data="admin_edit_task"
            )
        ],

        [
            InlineKeyboardButton(
                "📥 TASK REQUESTS",
                callback_data="admin_task_requests"
            ),
            InlineKeyboardButton(
                "🎁 GIFTS",
                callback_data="admin_gifts"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 ALL USERS",
                callback_data="admin_users"
            ),
            InlineKeyboardButton(
                "🚀 WITHDRAWALS",
                callback_data="admin_withdrawals"
            )
        ],

        [
            InlineKeyboardButton(
                "➕➖ USER BALANCE",
                callback_data="admin_balance"
            ),
            InlineKeyboardButton(
                "📜 WITHDRAWAL HISTORY",
                callback_data="admin_history"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 SET JOIN CHANNELS (UP TO 4)",
                callback_data="admin_set_channel"
            )
        ],
        [
            InlineKeyboardButton("⚙️ SETTINGS", callback_data="admin_settings"),
            InlineKeyboardButton("🏦 BOT FUND", callback_data="admin_fund")
        ],
        [
            InlineKeyboardButton("🛠 MAINTENANCE", callback_data="admin_maintenance")
        ],
        [
            InlineKeyboardButton("🆘 OFFICIAL SUPPORT", callback_data="admin_support"),
            InlineKeyboardButton("📣 PAYOUT CHANNEL", callback_data="admin_payout_channel")
        ],
        [InlineKeyboardButton("🏦 PAYMENT GATEWAY", callback_data="admin_gateways")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton("👮 ADD ADMINS", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("🎨 MAIN MENU EDITOR", callback_data="admin_menu_editor")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")],

        [
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_main"
            )
        ]

    ])


async def admin_control(update, context):

    clear_state(context)

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🤣 Nice try! This admin panel is only for the bot owner."
        )

        return

    await update.message.reply_text(

        "🛠 ADMIN CONTROL\n\n"
        "Choose an option:",

        reply_markup=admin_keyboard()
    )


# ============================================================


# PAYMENT GATEWAY HELPERS
def get_gateways():
    try:
        obj=json.loads(get_setting("payment_gateways") or "{}")
        return obj if isinstance(obj,dict) else {}
    except Exception:
        return {}

def gateway_admin_keyboard():
    rows=[[InlineKeyboardButton("➕ ADD GATEWAY",callback_data="admin_gateway_add")]]
    for name in get_gateways():
        rows.append([InlineKeyboardButton(f"🗑 {name}",callback_data=f"admin_gateway_delete:{name}")])
    rows.append([InlineKeyboardButton("⬅️ BACK",callback_data="back_admin")])
    return InlineKeyboardMarkup(rows)

# ADMIN CALLBACK
# ============================================================

async def admin_callback_refresh_channels(query):
    channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
    keyboard = []
    for i, channel in enumerate(channels):
        keyboard.append([InlineKeyboardButton(f"🗑 Delete channel {i + 1}: {channel[:35]}", callback_data=f"admin_channel_delete:{i}")])
    keyboard.extend([
        [InlineKeyboardButton("➕ ADD CHANNEL", callback_data="admin_channel_add")],
        [InlineKeyboardButton("✅ DONE", callback_data="admin_channel_done")],
        [InlineKeyboardButton("🚫 TURN OFF CHANNEL CHECK", callback_data="admin_channel_off")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")],
    ])
    listing = "\n".join(f"{i + 1}. {ch}" for i, ch in enumerate(channels)) or "No channels added yet."
    await query.message.reply_text("📢 JOIN CHANNEL SETTINGS\n\nCurrent channels:\n" + listing, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update, context):

    query = update.callback_query
    await query.answer()

    if not is_admin(
        query.from_user.id
    ):

        await query.message.reply_text(
            "⛔ Admin only."
        )

        return

    data = query.data
    if data == "admin_gateways":
        clear_state(context)
        names=list(get_gateways())
        await query.message.reply_text("🏦 PAYMENT GATEWAYS\\n\\n"+("\\n".join("• "+n for n in names) if names else "No gateways configured."),reply_markup=gateway_admin_keyboard())
        return
    if data == "admin_gateway_add":
        clear_state(context); context.user_data["admin_state"]="gateway_name"
        await query.message.reply_text("Send gateway name:",reply_markup=back_admin()); return
    if data.startswith("admin_gateway_delete:"):
        gateways=get_gateways(); gateways.pop(data.split(":",1)[1],None); set_setting("payment_gateways",json.dumps(gateways))
        await query.message.reply_text("Gateway deleted.",reply_markup=gateway_admin_keyboard()); return

    if data == "admin_broadcast_menu":
        clear_state(context)
        await query.message.reply_text("📢 BROADCAST OPTIONS", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 USER BROADCAST", callback_data="admin_user_broadcast")],
            [InlineKeyboardButton("📢 BOT BROADCAST", callback_data="admin_bot_broadcast")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")],
        ]))
        return
    if data == "admin_user_broadcast":
        clear_state(context); context.user_data["admin_state"]="user_broadcast_id"
        await query.message.reply_text("Send the target user's Telegram ID:",reply_markup=back_admin()); return
    if data == "admin_bot_broadcast":
        clear_state(context); context.user_data["admin_state"]="broadcast_message"
        await query.message.reply_text("📢 BOT BROADCAST\n\nSend the message to broadcast to all registered users:",reply_markup=back_admin()); return
    if data == "admin_broadcast":
        clear_state(context); context.user_data["admin_state"]="broadcast_message"
        await query.message.reply_text("Send the broadcast message:",reply_markup=back_admin()); return
    if data == "admin_manage_admins":
        if not is_owner(query.from_user.id):
            await query.answer("Only the bot owner can manage admins.", show_alert=True)
            return
        extras = sorted(get_extra_admin_ids())
        rows = [[InlineKeyboardButton("➕ ADD ADMIN", callback_data="admin_add_admin")]]
        for admin_id in extras:
            rows.append([InlineKeyboardButton(f"👤 SELECT ADMIN {admin_id}", callback_data=f"admin_select_admin:{admin_id}")])
        rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")])
        details = []
        for admin_id in extras:
            person = get_user(admin_id)
            display = (person["name"] if person else "Telegram user") or "Telegram user"
            username = (" @" + person["username"]) if person and person["username"] else ""
            details.append(f"• {display}{username} — ID: {admin_id}")
        await query.message.reply_text("👮 ADMIN LIST\n\n" + ("\n".join(details) if details else "No extra admins added yet."), reply_markup=InlineKeyboardMarkup(rows))
        return
    if data == "admin_add_admin":
        if not is_owner(query.from_user.id):
            await query.answer("Only the bot owner can add admins.", show_alert=True)
            return
        clear_state(context)
        context.user_data["admin_state"] = "add_admin_id"
        await query.message.reply_text("Send the Telegram user ID of the new admin:", reply_markup=back_admin())
        return
    if data.startswith("admin_select_admin:"):
        if not is_owner(query.from_user.id):
            await query.answer("Only the bot owner can manage admins.", show_alert=True)
            return
        try:
            selected_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid admin ID.", show_alert=True)
            return
        if selected_id not in get_extra_admin_ids():
            await query.answer("Admin not found.", show_alert=True)
            return
        person = get_user(selected_id)
        display = (person["name"] if person else "Telegram user") or "Telegram user"
        username = ("@" + person["username"]) if person and person["username"] else "Not available"
        await query.message.reply_text(
            f"👮 SELECTED ADMIN\n\nName: {display}\nUsername: {username}\nUser ID: {selected_id}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 REMOVE THIS ADMIN", callback_data=f"admin_remove_admin:{selected_id}")],
                [InlineKeyboardButton("⬅️ ADMIN LIST", callback_data="admin_manage_admins")],
            ])
        )
        return

    if data.startswith("admin_remove_admin:"):
        if not is_owner(query.from_user.id):
            await query.answer("Only the bot owner can remove admins.", show_alert=True)
            return
        try:
            remove_id = int(data.split(":", 1)[1])
            extras = get_extra_admin_ids()
            extras.discard(remove_id)
            set_setting("extra_admins", json.dumps(sorted(extras)))
        except (ValueError, TypeError):
            await query.answer("Invalid admin ID.", show_alert=True)
            return
        await query.answer("Admin removed.")
        # Re-render the admin list in this same admin chat message.
        extras = sorted(get_extra_admin_ids())
        rows = [[InlineKeyboardButton("➕ ADD ADMIN", callback_data="admin_add_admin")]]
        details = []
        for admin_id in extras:
            person = get_user(admin_id)
            display = (person["name"] if person else "Telegram user") or "Telegram user"
            username = (" @" + person["username"]) if person and person["username"] else ""
            details.append(f"• {display}{username} — ID: {admin_id}")
            rows.append([InlineKeyboardButton(f"👤 SELECT ADMIN {admin_id}", callback_data=f"admin_select_admin:{admin_id}")])
        rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")])
        await query.message.edit_text("👮 ADMIN LIST\n\n" + ("\n".join(details) if details else "No extra admins added yet."), reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "admin_support":
        raw = get_setting("official_support") or "[]"
        try: entries = json.loads(raw)
        except Exception: entries = []
        rows = [[InlineKeyboardButton(f"🗑 {x.get('name','Support')}", callback_data=f"admin_support_del:{i}")] for i,x in enumerate(entries)]
        rows += [[InlineKeyboardButton("➕ ADD SUPPORT CONTACT", callback_data="admin_support_add")], [InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")]]
        listing = "\n".join(f"{i+1}. {x.get('name')} → {x.get('link')}" for i,x in enumerate(entries)) or "No support contacts saved."
        await query.message.reply_text("🆘 OFFICIAL SUPPORT SETTINGS\n\n"+listing, reply_markup=InlineKeyboardMarkup(rows))
        return
    if data == "admin_support_add":
        clear_state(context); context.user_data["admin_state"] = "support_name"
        await query.message.reply_text("Send the support contact name:", reply_markup=back_admin()); return
    if data.startswith("admin_support_del:"):
        try:
            idx=int(data.split(":",1)[1]); entries=json.loads(get_setting("official_support") or "[]")
            if 0 <= idx < len(entries): entries.pop(idx); set_setting("official_support", json.dumps(entries))
        except Exception: pass
        await query.message.reply_text("Support contact removed.", reply_markup=admin_keyboard()); return
    if data == "admin_payout_channel":
        clear_state(context); context.user_data["admin_state"] = "payout_channel"
        current=get_setting("payout_channel") or "Not set"
        await query.message.reply_text(f"Current payout channel: {current}\n\nSend the channel ID (e.g. -1001234567890) or @channelusername. Send OFF to disable.", reply_markup=back_admin()); return
    if data == "admin_menu_editor":
        await query.message.reply_text(
            "🎨 MAIN MENU EDITOR\n\nORDER / SELECT BUTTON\nTap one button below, then rename it, choose its full background colour, or move it with the arrows. Save & Apply publishes changes for everyone.",
            reply_markup=menu_editor_keyboard()
        )
        return
    if data == "menu_save":
        await query.message.reply_text(
            "✅ Main menu changes saved. All users will see the updated menu the next time the bot sends or refreshes their main menu.",
            reply_markup=main_menu(query.from_user.id)
        )
        return
    if data.startswith("menu_pick:"):
        key = data.split(":", 1)[1]
        context.user_data["menu_edit_key"] = key
        try:
            labels = json.loads(get_setting("menu_labels") or "{}")
        except Exception:
            labels = {}
        label = labels.get(key, MENU_LABELS.get(key, key))
        await query.message.reply_text(
            f"Selected button: {label}\nChoose what to change:",
            reply_markup=menu_editor_action_keyboard(key)
        )
        return
    if data.startswith("menu_color:"):
        try:
            _, key, chosen = data.split(":", 2)
            colors = json.loads(get_setting("menu_colors") or "{}")
        except Exception:
            await query.answer("Could not update colour", show_alert=True)
            return
        if chosen == "none":
            colors.pop(key, None)
        elif chosen in ("blue", "green", "red"):
            colors[key] = chosen
        else:
            await query.answer("Invalid colour", show_alert=True)
            return
        set_setting("menu_colors", json.dumps(colors))
        context.user_data["menu_edit_key"] = key
        labels = json.loads(get_setting("menu_labels") or "{}")
        label = labels.get(key, MENU_LABELS.get(key, key))
        await query.message.reply_text(
            f"✅ {label} colour set to {chosen.upper()}.\nSelect another action or save.",
            reply_markup=menu_editor_action_keyboard(key)
        )
        return
    if data == "menu_rename":
        key = context.user_data.get("menu_edit_key")
        if not key:
            await query.message.reply_text("Select a button first.", reply_markup=menu_editor_keyboard())
            return
        clear_state(context)
        context.user_data["menu_edit_key"] = key
        context.user_data["admin_state"] = "menu_rename"
        await query.message.reply_text("Send the new button name:", reply_markup=back_admin())
        return
    if data.startswith("menu_move:"):
        _, key, direction = data.split(":")
        try:
            layout = json.loads(get_setting("menu_layout") or json.dumps(MENU_DEFAULT))
        except Exception:
            layout = [row[:] for row in MENU_DEFAULT]
        # Find the selected button's current row/column.
        position = None
        for ri, row in enumerate(layout):
            if key in row:
                position = (ri, row.index(key))
                break
        if position is not None:
            ri, ci = position
            target = None
            if direction in ("left", "right"):
                ni, nj = ri, ci + (-1 if direction == "left" else 1)
                if 0 <= nj < len(layout[ni]):
                    target = (ni, nj)
            elif direction in ("up", "down"):
                ni = ri + (-1 if direction == "up" else 1)
                if 0 <= ni < len(layout):
                    # Choose the closest column in the adjacent row.
                    nj = min(ci, len(layout[ni]) - 1)
                    if layout[ni]:
                        target = (ni, nj)
            if target:
                ti, tj = target
                layout[ri][ci], layout[ti][tj] = layout[ti][tj], layout[ri][ci]
                set_setting("menu_layout", json.dumps(layout))
        context.user_data["menu_edit_key"] = key
        await query.message.reply_text(
            "Position updated. Continue editing or press Save & Apply.",
            reply_markup=menu_editor_action_keyboard(key)
        )
        return

    if data == "admin_settings":
        enabled = get_setting("withdraw_enabled") != "0"
        upi_enabled = get_setting("upi_enabled") != "0"
        await query.message.reply_text(
            f"⚙️ SETTINGS\nWithdrawals: {'ON' if enabled else 'OFF'}\nUPI Payment Method: {'ON' if upi_enabled else 'OFF'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Toggle withdrawals", callback_data="admin_toggle_withdraw")],
                [InlineKeyboardButton("💳 PAYMENT SETTINGS", callback_data="admin_payment_settings")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")]
            ])
        )
    elif data == "admin_payment_settings":
        upi_enabled = get_setting("upi_enabled") != "0"
        await query.message.reply_text(
            f"💳 PAYMENT SETTINGS\nUPI method: {'ON' if upi_enabled else 'OFF'}\nWhen OFF, the PAYOUT METHOD menu button remains visible and shows a closed message. Withdrawals are blocked.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟢 UPI ON" if not upi_enabled else "🔴 UPI OFF", callback_data="admin_toggle_upi")],
                [InlineKeyboardButton("⬅️ BACK TO SETTINGS", callback_data="admin_settings")]
            ])
        )
    elif data == "admin_toggle_upi":
        set_setting("upi_enabled", "0" if get_setting("upi_enabled") != "0" else "1")
        await query.message.reply_text(f"✅ UPI payment method is now {'OFF' if get_setting('upi_enabled') == '0' else 'ON'}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 PAYMENT SETTINGS", callback_data="admin_payment_settings")], [InlineKeyboardButton("⬅️ SETTINGS", callback_data="admin_settings")]]))
    elif data == "admin_toggle_withdraw":
        set_setting("withdraw_enabled", "0" if get_setting("withdraw_enabled") != "0" else "1")
        await query.message.reply_text(f"✅ Withdrawals now {'OFF' if get_setting('withdraw_enabled') == '0' else 'ON'}.", reply_markup=admin_keyboard())
    elif data == "admin_fund":
        clear_state(context)
        context.user_data["admin_state"] = "set_bot_fund"
        current = get_setting("bot_fund") or "0"
        await query.message.reply_text(f"🏦 Current bot fund: ₹{current}\nSend the new total fund amount:", reply_markup=back_admin())
    elif data == "admin_maintenance":
        state = get_setting("maintenance") == "1"
        if state:
            set_setting("maintenance", "0")
            set_setting("maintenance_until", "")
            await query.message.reply_text("✅ Maintenance turned OFF.", reply_markup=admin_keyboard())
        else:
            clear_state(context)
            context.user_data["admin_state"] = "maintenance_until"
            await query.message.reply_text("Send maintenance expected end time (example: Today 6:30 PM):", reply_markup=back_admin())
    elif data == "admin_set_channel":
        clear_state(context)
        channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
        keyboard = []
        for i, channel in enumerate(channels):
            keyboard.append([InlineKeyboardButton(f"🗑 Delete channel {i + 1}: {channel[:35]}", callback_data=f"admin_channel_delete:{i}")])
        keyboard.append([InlineKeyboardButton("➕ ADD CHANNEL", callback_data="admin_channel_add")])
        keyboard.append([InlineKeyboardButton("✅ DONE", callback_data="admin_channel_done")])
        keyboard.append([InlineKeyboardButton("🚫 TURN OFF CHANNEL CHECK", callback_data="admin_channel_off")])
        keyboard.append([InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")])
        channel_text = "\n".join(f"{i + 1}. {ch}" for i, ch in enumerate(channels)) or "No channels added yet."
        await query.message.reply_text(
            "📢 JOIN CHANNEL SETTINGS\n\n"
            f"Current channels:\n{channel_text}\n\n"
            "Tap ADD CHANNEL to add a link, or tap a delete button to remove one.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "admin_channel_add":
        clear_state(context)
        context.user_data["admin_state"] = "set_channel_add_link"
        await query.message.reply_text("➕ Send the channel link (example: https://t.me/yourchannel).", reply_markup=back_admin())

    elif data == "admin_channel_delete":
        channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
        if channels:
            db.execute("DELETE FROM settings WHERE key='required_channels'")
            db.commit()
        await query.message.reply_text("No channels remain.", reply_markup=admin_keyboard())

    elif data.startswith("admin_channel_delete:"):
        try:
            index = int(data.split(":", 1)[1])
            channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
            if 0 <= index < len(channels):
                removed = channels.pop(index)
                if channels:
                    set_setting("required_channels", ",".join(channels))
                else:
                    db.execute("DELETE FROM settings WHERE key='required_channels'")
                    db.commit()
                await query.message.reply_text(f"🗑 Removed channel: {removed}")
            await admin_callback_refresh_channels(query)
        except Exception as exc:
            logging.exception("Channel deletion failed: %s", exc)
            await query.message.reply_text("Could not delete that channel. Please try again.")

    elif data == "admin_channel_done":
        channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
        await query.message.reply_text(f"✅ Channel settings saved successfully.\nActive channels: {len(channels)}", reply_markup=admin_keyboard())

    elif data == "admin_channel_off":
        db.execute("DELETE FROM settings WHERE key='required_channels'")
        db.commit()
        await query.message.reply_text("✅ Join-channel requirement is OFF.", reply_markup=admin_keyboard())

    elif data == "admin_add_task":

        clear_state(context)
        context.user_data["admin_state"] = "task_name"

        await query.message.reply_text(

            "➕ ADD BOT TASK\n\n"
            "Step 1/3\n\n"
            "Enter task name:",

            reply_markup=back_admin()
        )

    elif data == "admin_edit_task":

        tasks = db.execute("""
            SELECT *
            FROM tasks
            ORDER BY id DESC
        """).fetchall()

        if not tasks:

            await query.message.reply_text(
                "No tasks found.",
                reply_markup=back_admin()
            )

            return

        keyboard = []

        for task in tasks:

            keyboard.append([
                InlineKeyboardButton(
                    f"✏️ {task['name']}",
                    callback_data=f"edit_task:{task['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_admin"
            )
        ])

        await query.message.reply_text(
            "✏️ EDIT / DELETE TASK\n\nSelect task:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

    elif data == "admin_task_requests":

        requests = db.execute("""
            SELECT
                r.*,
                t.name AS task_name,
                u.name AS user_name,
                u.username
            FROM task_requests r

            LEFT JOIN tasks t
            ON t.id=r.task_id

            LEFT JOIN users u
            ON u.user_id=r.user_id

            WHERE r.status='PENDING'

            ORDER BY r.id DESC
        """).fetchall()

        if not requests:

            await query.message.reply_text(
                "📥 TASK REQUESTS\n\n"
                "No pending task submissions.",
                reply_markup=back_admin()
            )

            return

        for r in requests:

            text = (

                f"📥 TASK REQUEST <code>#{r['id']}</code>\n\n"
                f"📋 Task: {html.escape(str(r['task_name'] or 'Task'))}\n"
                f"💰 Reward: {money(r['reward'])}\n\n"
                f"👤 User ID: <code>{r['user_id']}</code>\n"
                f"Name: {html.escape(str(r['user_name'] or '-'))}\n"
                f"Username: @{html.escape(str(r['username'] or '-'))}\n\n"
                f"Type: {html.escape(str(r['request_type']))}\n"
                f"🕒 Submitted: {html.escape(str(r['created_at']))}\n"
            )

            if r["submission"]:

                text += (
                    f"\n📨 Submission:\n"
                    f"{html.escape(str(r['submission']))}\n"
                )

            if r["screenshot_file_id"]:

                text += "\n📸 Screenshot attached."

            await query.message.reply_text(

                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ APPROVE",
                            callback_data=f"taskapprove:{r['id']}"
                        ),
                        InlineKeyboardButton(
                            "❌ REJECT",
                            callback_data=f"taskreject:{r['id']}"
                        )
                    ]
                ])
            )

            if r["screenshot_file_id"]:

                try:

                    await context.bot.send_photo(
                        query.from_user.id,
                        r["screenshot_file_id"],
                        caption=(
                            f"📸 Screenshot\n"
                            f"Task Request #{r['id']}"
                        )
                    )

                except:
                    pass

    elif data == "admin_gifts":

        gifts = db.execute("""
            SELECT *
            FROM gifts
            ORDER BY id DESC
        """).fetchall()

        if not gifts:

            await query.message.reply_text(
                "🎁 No gifts created.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "➕ ADD GIFT",
                            callback_data="admin_add_gift"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ BACK",
                            callback_data="back_admin"
                        )
                    ]
                ])
            )

            return

        keyboard = []

        for gift in gifts:

            keyboard.append([
                InlineKeyboardButton(
                    f"🎁 {gift['code']} • {money(gift['amount'])}",
                    callback_data=f"edit_gift:{gift['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "➕ ADD GIFT",
                callback_data="admin_add_gift"
            )
        ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_admin"
            )
        ])

        await query.message.reply_text(
            "🎁 GIFT MANAGEMENT\n\n"
            "Tap a gift to edit or delete it.",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

    elif data == "admin_add_gift":

        clear_state(context)
        context.user_data["admin_state"] = "gift_code"

        await query.message.reply_text(

            "🎁 ADD GIFT\n\n"
            "Step 1/4\n\n"
            "Enter gift code:",

            reply_markup=back_admin()
        )

    elif data == "admin_users":

        users = db.execute("""
            SELECT *
            FROM users
            ORDER BY created_at DESC
        """).fetchall()

        if not users:

            await query.message.reply_text(
                "No users.",
                reply_markup=back_admin()
            )

            return

        top_users = users[:3]
        text = "👥 ALL USERS (Top 3 Preview)\n\n"

        for u in top_users:
            text += (
                f"🆔 {u['user_id']}\n"
                f"👤 {u['name']}\n"
                f"💰 {money(u['balance'])}\n"
                f"💳 {u['upi'] or 'UPI not set'}\n"
                f"────────────\n"
            )

        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👁️ All View", callback_data="admin_users_all")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_admin")]
            ])
        )

    elif data == "admin_users_all":

        users = db.execute("""
            SELECT *
            FROM users
            ORDER BY created_at DESC
        """).fetchall()

        text = "👥 ALL USERS (Complete List)\n\n"

        for u in users:
            text += (
                f"🆔 {u['user_id']}\n"
                f"👤 {u['name']}\n"
                f"💰 {money(u['balance'])}\n"
                f"💳 {u['upi'] or 'UPI not set'}\n"
                f"────────────\n"
            )

            if len(text) > 3500:
                await query.message.reply_text(text)
                text = "👥 ALL USERS (Continuation)\n\n"

        if text.strip():
            await query.message.reply_text(
                text,
                reply_markup=back_admin()
            )

    elif data == "admin_withdrawals":

        rows = db.execute("""
            SELECT
                w.*,
                u.name,
                u.username
            FROM withdrawals w

            LEFT JOIN users u
            ON u.user_id=w.user_id

            WHERE w.status='PENDING'

            ORDER BY w.id DESC
        """).fetchall()

        if not rows:

            await query.message.reply_text(
                "🚀 No pending withdrawals.",
                reply_markup=back_admin()
            )

            return

        for w in rows:

            await query.message.reply_text(

                f"🚀 WITHDRAWAL #{w['id']}\n\n"
                f"User ID: <code>{w['user_id']}</code>\n"
                f"Name: {html.escape(str(w['name'] or '-'))}\n"
                f"Amount: {money(w['amount'])}\n"
                f"Method: {html.escape(str(w['method']))}\n"
                f"UPI: <code>{html.escape(str(w['payment']))}</code>\n"
                f"Status: {html.escape(str(w['status']))}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ APPROVE",
                            callback_data=f"approve:{w['id']}"
                        ),
                        InlineKeyboardButton(
                            "❌ REJECT",
                            callback_data=f"reject:{w['id']}"
                        )
                    ]
                ])
            )

    elif data == "admin_balance":

        clear_state(context)
        context.user_data["admin_state"] = "balance_user"

        await query.message.reply_text(

            "➕➖ USER BALANCE\n\n"
            "Enter User ID:",

            reply_markup=back_admin()
        )

    elif data == "admin_history":

        rows = db.execute("""
            SELECT w.*, u.name AS user_name
            FROM withdrawals w
            LEFT JOIN users u ON u.user_id=w.user_id
            ORDER BY w.id DESC
            LIMIT 100
        """).fetchall()

        if not rows:

            await query.message.reply_text(
                "📜 No withdrawal history.",
                reply_markup=back_admin()
            )

            return

        text = "📜 WITHDRAWAL HISTORY\n\n"

        for w in rows:

            text += (

                f"#{w['id']} | {w['status']}\n"
                f"User: {w['user_name'] or 'Unknown'} (ID {w['user_id']})\n"
                f"Amount: {money(w['amount'])}\n"
                f"UPI: {w['payment']}\n"
                f"────────────\n"
            )

            if len(text) > 3500:

                await query.message.reply_text(text)
                text = "📜 HISTORY\n\n"

        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Clear History Messages", callback_data="admin_clear_history")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_admin")]
            ])
        )

    elif data == "admin_clear_history":
        db.execute("DELETE FROM withdrawals WHERE status != 'PENDING'")
        db.commit()
        await query.message.reply_text("✅ Completed/Rejected withdrawal history cleared successfully.", reply_markup=back_admin())

    elif data.startswith("edit_gift:"):

        gift_id = int(
            data.split(":")[1]
        )

        gift = db.execute("""
            SELECT *
            FROM gifts
            WHERE id=?
        """, (gift_id,)).fetchone()

        if not gift:

            await query.message.reply_text(
                "❌ Gift not found.",
                reply_markup=back_admin()
            )

            return

        await query.message.reply_text(

            f"🎁 GIFT\n\n"
            f"Code: {gift['code']}\n"
            f"Name: {gift['name']}\n"
            f"Reward: {money(gift['amount'])}\n"
            f"Per account: {gift['per_account']}\n"
            f"Status: {'ACTIVE' if gift['active'] else 'OFF'}",

            reply_markup=InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(
                        "✏️ CHANGE NAME",
                        callback_data=f"giftname:{gift_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "💰 CHANGE AMOUNT",
                        callback_data=f"giftamount:{gift_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "👤 CHANGE LIMIT",
                        callback_data=f"giftlimit:{gift_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🔄 ON / OFF",
                        callback_data=f"gifttoggle:{gift_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🗑️ DELETE GIFT",
                        callback_data=f"giftdel:{gift_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data="admin_gifts"
                    )
                ]

            ])
        )

    elif data.startswith("giftdel:"):
        gift_id = int(data.split(":")[1])
        db.execute("DELETE FROM gifts WHERE id=?", (gift_id,))
        db.execute("DELETE FROM gift_claims WHERE gift_id=?", (gift_id,))
        db.commit()
        await query.message.reply_text("✅ Gift deleted successfully.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Gifts", callback_data="admin_gifts")]]))

    elif data.startswith("edit_task:"):

        task_id = int(
            data.split(":")[1]
        )

        task = db.execute("""
            SELECT *
            FROM tasks
            WHERE id=?
        """, (task_id,)).fetchone()

        if not task:

            await query.message.reply_text(
                "❌ Task not found.",
                reply_markup=back_admin()
            )

            return

        await query.message.reply_text(

            f"✏️ TASK EDIT\n\n"
            f"Name: {task['name']}\n"
            f"Reward: {money(task['amount'])}\n"
            f"Link: {task['link']}\n"
            f"Status: {'ACTIVE' if task['active'] else 'OFF'}",

            reply_markup=InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(
                        "✏️ NAME",
                        callback_data=f"taskeditname:{task_id}"
                    ),
                    InlineKeyboardButton(
                        "💰 AMOUNT",
                        callback_data=f"taskeditamount:{task_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🔗 LINK",
                        callback_data=f"taskeditlink:{task_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🔄 ON / OFF",
                        callback_data=f"tasktoggle:{task_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🗑️ DELETE TASK",
                        callback_data=f"taskdel:{task_id}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data="admin_edit_task"
                    )
                ]

            ])
        )

    elif data.startswith("taskdel:"):
        task_id = int(data.split(":")[1])
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        db.execute("DELETE FROM task_claims WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM task_requests WHERE task_id=?", (task_id,))
        db.commit()
        await query.message.reply_text("✅ Task deleted successfully.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Tasks", callback_data="admin_edit_task")]]))

    elif data.startswith("giftname:"):

        gift_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_gift_name"
        context.user_data["gift_id"] = gift_id

        await query.message.reply_text(
            "✏️ Enter new gift name:",
            reply_markup=back_admin()
        )

    elif data.startswith("giftamount:"):

        gift_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_gift_amount"
        context.user_data["gift_id"] = gift_id

        await query.message.reply_text(
            "💰 Enter new gift amount:",
            reply_markup=back_admin()
        )

    elif data.startswith("giftlimit:"):

        gift_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_gift_limit"
        context.user_data["gift_id"] = gift_id

        await query.message.reply_text(
            "👤 Enter new per-account limit:",
            reply_markup=back_admin()
        )

    elif data.startswith("gifttoggle:"):

        gift_id = int(
            data.split(":")[1]
        )

        db.execute("""
            UPDATE gifts
            SET active =
                CASE
                    WHEN active=1 THEN 0
                    ELSE 1
                END
            WHERE id=?
        """, (gift_id,))

        db.commit()

        await query.message.reply_text(
            "✅ Gift status updated.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ BACK TO GIFTS",
                        callback_data="admin_gifts"
                    )
                ]
            ])
        )

    elif data.startswith("taskeditname:"):

        task_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_task_name"
        context.user_data["task_id"] = task_id

        await query.message.reply_text(
            "✏️ Enter new task name:",
            reply_markup=back_admin()
        )

    elif data.startswith("taskeditamount:"):

        task_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_task_amount"
        context.user_data["task_id"] = task_id

        await query.message.reply_text(
            "💰 Enter new reward:",
            reply_markup=back_admin()
        )

    elif data.startswith("taskeditlink:"):

        task_id = int(
            data.split(":")[1]
        )

        clear_state(context)
        context.user_data["admin_state"] = "edit_task_link"
        context.user_data["task_id"] = task_id

        await query.message.reply_text(
            "🔗 Enter new task link:",
            reply_markup=back_admin()
        )

    elif data.startswith("tasktoggle:"):

        task_id = int(
            data.split(":")[1]
        )

        db.execute("""
            UPDATE tasks
            SET active =
                CASE
                    WHEN active=1 THEN 0
                    ELSE 1
                END
            WHERE id=?
        """, (task_id,))

        db.commit()

        await query.message.reply_text(
            "✅ Task status updated.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ BACK TO TASKS",
                        callback_data="admin_edit_task"
                    )
                ]
            ])
        )


# ============================================================
# ADMIN TASK REQUEST APPROVE / REJECT
# ============================================================

async def process_task_request(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return
    await query.answer()
    action, request_id_text = query.data.split(":", 1)
    try:
        request_id = int(request_id_text)
    except ValueError:
        await query.answer("Invalid request ID.", show_alert=True)
        return

    # Serialize approval/rejection and make the status + reward update indivisible.
    try:
        db.execute("BEGIN IMMEDIATE")
        request = db.execute("""
            SELECT r.*, COALESCE(t.name, 'Task') AS task_name
            FROM task_requests r LEFT JOIN tasks t ON t.id=r.task_id
            WHERE r.id=?
        """, (request_id,)).fetchone()
        if not request:
            db.rollback()
            await query.message.reply_text("❌ Request not found.")
            return
        if request['status'] != 'PENDING':
            db.rollback()
            await query.message.reply_text(f"⚠️ Request #{request_id} already processed: {request['status']}.")
            try: await query.message.edit_reply_markup(reply_markup=None)
            except Exception: pass
            return

        new_status = 'APPROVED' if action == 'taskapprove' else 'REJECTED' if action == 'taskreject' else None
        if not new_status:
            db.rollback()
            await query.answer("Invalid action.", show_alert=True)
            return
        changed = db.execute("""
            UPDATE task_requests SET status=?, processed_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='PENDING'
        """, (new_status, request_id)).rowcount
        if changed != 1:
            db.rollback()
            await query.message.reply_text("⚠️ This request was already processed.")
            return

        if new_status == 'APPROVED':
            credited = db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                                  (request['reward'], request['user_id'])).rowcount
            if credited != 1:
                db.rollback()
                await query.message.reply_text("❌ User account not found. Approval cancelled.")
                return
            db.execute("INSERT OR IGNORE INTO task_claims(task_id,user_id) VALUES(?,?)",
                       (request['task_id'], request['user_id']))
        db.commit()
    except Exception:
        db.rollback()
        logging.exception("Atomic task request processing failed")
        await query.message.reply_text("❌ Processing failed. No reward/status change was committed.")
        return

    stamp = local_timestamp()
    if new_status == 'APPROVED':
        admin_msg = f"✅ TASK REQUEST #{request_id} APPROVED\nReward added: {money(request['reward'])}\nProcessed: {stamp}"
        user_msg = f"🎉 TASK APPROVED!\n\nTask: {request['task_name']}\nReward added: {money(request['reward'])}\nProcessed: {stamp}\n\nThank you!"
    else:
        admin_msg = f"❌ TASK REQUEST #{request_id} REJECTED\nNo reward added.\nProcessed: {stamp}"
        user_msg = f"❌ TASK REJECTED\n\nTask: {request['task_name']}\nRequest: #{request_id}\nProcessed: {stamp}\n\nNo reward was added."
    await query.message.reply_text(admin_msg)
    try:
        await context.bot.send_message(request['user_id'], user_msg)
    except Exception:
        logging.exception("Could not notify user about task request outcome")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ============================================================
# WITHDRAW APPROVE / REJECT
# ============================================================

async def process_withdrawal(update, context):
    query = update.callback_query
    action, withdrawal_id = query.data.split(":")
    withdrawal_id = int(withdrawal_id)

    if not is_admin(query.from_user.id):
        await query.answer("⛔ You are not an admin.", show_alert=True)
        return

    request = db.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
    if not request:
        await query.answer("Withdrawal request not found.", show_alert=True)
        return

    if request["status"] != "PENDING":
        await query.answer(f"Already {request['status'].lower()}.", show_alert=True)
        # Keep the original post/message and remove action buttons; never create a duplicate chat message.
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "approve":
        gateway_response_text = "Manual approval"
        if request["method"] != "UPI":
            endpoint=get_gateways().get(request["gateway_name"] or request["method"])
            if not endpoint:
                await query.answer("Gateway configuration missing. Request remains pending.",show_alert=True); return
            try:
                url=endpoint.format(number=urllib.parse.quote(str(request["payment"]),safe=""),amount=urllib.parse.quote(f"{float(request['amount']):.2f}",safe=""),comment=urllib.parse.quote(f"Withdrawal #{withdrawal_id}",safe=""))
                def _get():
                    response = requests.get(url, headers={"User-Agent":"TelegramBot/1.0"}, timeout=20)
                    response.raise_for_status()
                    return response.text
                gateway_response_text=await asyncio.to_thread(_get)
                parsed=json.loads(gateway_response_text)
                response_status=str(parsed.get("status",parsed.get("success",parsed.get("result","")))).lower()
                if response_status not in ("success","successful","ok","true","1"):
                    await query.answer("Gateway response not successful; request remains pending.",show_alert=True); return
            except Exception:
                logging.exception("Gateway payout error")
                await query.answer("Gateway request failed; pending unchanged.",show_alert=True); return
        db.execute("UPDATE withdrawals SET status='APPROVED', processed_at=CURRENT_TIMESTAMP WHERE id=? AND status='PENDING'", (withdrawal_id,))
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            await query.answer("This request has already been processed.", show_alert=True); return
        db.commit()
        status_line = f"approved by @{query.from_user.username or query.from_user.id} at {local_timestamp()}"
        recipient = get_user(request["user_id"])
        response_payload = json.dumps({"message": f"Sent ₹{float(request['amount']):.2f} to {recipient['name'] if recipient else request['user_id']}", "status": "success"}, ensure_ascii=False)
        user_text = (f"✅ Withdrawal request approvale ✅\n\n🟢 User : <code>{request['user_id']}</code>\n"
                     f"🤘 Remaining Balance :- {money(updated_user['balance'] if 'updated_user' in locals() else (get_user(request['user_id'])['balance']) )}\n\n"
                     f"🚀 Amount : {money(request['amount'])} INR (-)\n⛔ Address : <code>{html.escape(str(request['payment']))}</code>\n\n"
                     f"💡 Bot: @{context.bot.username or 'bot'}\n\n⚠️ {html.escape(str(request['method']))} Response: <code>{html.escape(response_payload)}</code>")
        gateway_response_text = response_payload
    elif action == "reject":
        db.execute("UPDATE withdrawals SET status='REJECTED', processed_at=CURRENT_TIMESTAMP WHERE id=? AND status='PENDING'", (withdrawal_id,))
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            await query.answer("This request has already been processed.", show_alert=True); return
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (request["amount"], request["user_id"]))
        restored_fund = float(get_setting("bot_fund") or 0) + float(request["amount"])
        set_setting("bot_fund", f"{restored_fund:.2f}")
        if restored_fund > 0: set_setting("withdraw_enabled", "1")
        db.commit()
        status_line = f"rejected by @{query.from_user.username or query.from_user.id} at {local_timestamp()}"
        recipient = get_user(request["user_id"])
        response_payload = json.dumps({"message": f"Withdrawal rejected; ₹{float(request['amount']):.2f} refunded to {recipient['name'] if recipient else request['user_id']}", "status": "rejected"}, ensure_ascii=False)
        user_text = (f"✅ Withdrawal request rejected ❌\n\n🟢 User : <code>{request['user_id']}</code>\n"
                     f"🤘 Remaining Balance :- {money(get_user(request['user_id'])['balance'])}\n\n"
                     f"🚀 Amount : {money(request['amount'])} INR (-)\n⛔ Address : <code>{html.escape(str(request['payment']))}</code>\n\n"
                     f"💡 Bot: @{context.bot.username or 'bot'}\n\n⚠️ {html.escape(str(request['method']))} Response: <code>{html.escape(response_payload)}</code>")
        gateway_response_text = response_payload
    else:
        await query.answer("Invalid action.", show_alert=True); return

    updated_user = get_user(request["user_id"])

    try:
        await context.bot.send_message(request["user_id"], user_text, parse_mode="HTML")
    except Exception:
        logging.exception("Could not notify user about withdrawal status")

    channel_text = user_text
    try:
        if request["payout_chat_id"] and request["payout_message_id"]:
            await context.bot.edit_message_text(chat_id=request["payout_chat_id"], message_id=request["payout_message_id"], text=channel_text, parse_mode="HTML", reply_markup=None)
    except Exception:
        logging.exception("Could not update original payout-channel message")

    # If the action was clicked on the original admin DM/channel post, edit it in place too.
    try:
        await query.message.edit_text(channel_text, parse_mode="HTML", reply_markup=None)
    except Exception:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await query.answer(f"Withdrawal {request['status'].lower()}.")


# ============================================================
# ADMIN TEXT INPUT
# ============================================================

async def admin_callback_refresh_channels_for_message(update, context):
    channels = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
    keyboard = []
    for i, channel in enumerate(channels):
        keyboard.append([InlineKeyboardButton(f"🗑 Delete channel {i + 1}: {channel[:35]}", callback_data=f"admin_channel_delete:{i}")])
    keyboard.extend([
        [InlineKeyboardButton("➕ ADD CHANNEL", callback_data="admin_channel_add")],
        [InlineKeyboardButton("✅ DONE", callback_data="admin_channel_done")],
        [InlineKeyboardButton("🚫 TURN OFF CHANNEL CHECK", callback_data="admin_channel_off")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="back_admin")],
    ])
    listing = "\n".join(f"{i + 1}. {ch}" for i, ch in enumerate(channels)) or "No channels added yet."
    await update.message.reply_text("📢 JOIN CHANNEL SETTINGS\n\nCurrent channels:\n" + listing, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_text(update, context):

    if not is_admin(
        update.effective_user.id
    ):

        return False

    state = context.user_data.get(
        "admin_state"
    )

    if not state:

        return False

    text = update.message.text.strip()

    if state == "gateway_name":
        if not text or len(text)>40:
            await update.message.reply_text("Name must be 1–40 characters.",reply_markup=back_admin()); return True
        context.user_data["gateway_new_name"]=text
        context.user_data["admin_state"]="gateway_endpoint"
        await update.message.reply_text("Send API URL template with {number}, {amount}, {comment}. Keep tokens private.",reply_markup=back_admin()); return True
    if state == "gateway_endpoint":
        endpoint=text.strip()
        if not endpoint.startswith(("https://","http://")) or not all(x in endpoint for x in ("{number}","{amount}","{comment}")):
            await update.message.reply_text("URL template must contain {number}, {amount}, {comment}.",reply_markup=back_admin()); return True
        gateways=get_gateways(); name=context.user_data.get("gateway_new_name","Gateway")
        gateways[name]=endpoint; set_setting("payment_gateways",json.dumps(gateways)); clear_state(context)
        await update.message.reply_text(f"✅ Gateway {name} saved.",reply_markup=gateway_admin_keyboard()); return True

    if state == "user_broadcast_id":
        try: target_id=int(text)
        except (ValueError, TypeError):
            await update.message.reply_text("Send a valid numeric Telegram user ID:",reply_markup=back_admin()); return True
        context.user_data["broadcast_target_id"]=target_id
        context.user_data["admin_state"]="user_broadcast_content"
        await update.message.reply_text("Now send the message (text, photo, video, document, etc.) to deliver to that user:",reply_markup=back_admin()); return True

    if state == "user_broadcast_content":
        target_id=context.user_data.get("broadcast_target_id")
        clear_state(context)
        try:
            await update.message.copy(chat_id=target_id)
            await update.message.reply_text(f"✅ Message sent to user {target_id}.",reply_markup=admin_keyboard())
        except Exception as exc:
            logging.warning("User broadcast failed for %s: %s",target_id,exc)
            await update.message.reply_text("❌ Could not send message to that user.",reply_markup=admin_keyboard())
        return True

    if state == "broadcast_message":
        clear_state(context)
        rows = db.execute("SELECT user_id FROM users").fetchall()
        sent, failed = 0, 0
        for row in rows:
            try:
                await update.message.copy(chat_id=row["user_id"])
                sent += 1
            except Exception as exc:
                logging.warning("Broadcast failed for %s: %s", row["user_id"], exc)
                failed += 1
        await update.message.reply_text(f"📢 BOT BROADCAST FINISHED\n\nDelivered: {sent}\nFailed: {failed}", reply_markup=admin_keyboard())
        return True

    if state == "add_admin_id":
        if not is_owner(update.effective_user.id):
            clear_state(context)
            await update.message.reply_text("⛔ Only the bot owner can add admins.")
            return True
        try:
            new_admin_id = int(text)
            if new_admin_id <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a valid numeric Telegram user ID.", reply_markup=back_admin())
            return True
        if new_admin_id in ADMIN_IDS:
            await update.message.reply_text("ℹ️ This user is already a bot owner.", reply_markup=admin_keyboard())
            clear_state(context)
            return True
        extras = get_extra_admin_ids()
        extras.add(new_admin_id)
        set_setting("extra_admins", json.dumps(sorted(extras)))
        clear_state(context)
        person = get_user(new_admin_id)
        display = (person["name"] if person else "Telegram user") or "Telegram user"
        await update.message.reply_text(f"✅ Admin added.\nName: {display}\nUser ID: {new_admin_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👮 ADMIN LIST", callback_data="admin_manage_admins")], [InlineKeyboardButton("⬅️ ADMIN PANEL", callback_data="back_admin")]]))
        return True

    if state == "support_name":
        context.user_data["support_new_name"] = text
        context.user_data["admin_state"] = "support_link"
        await update.message.reply_text("Now send the Telegram username or channel link (for example @username or https://t.me/username):", reply_markup=back_admin()); return True
    if state == "support_link":
        link=text.strip()
        if link.startswith("@"):
            link="https://t.me/"+link[1:]
        if not (link.startswith("https://t.me/") or link.startswith("http://t.me/")):
            await update.message.reply_text("Invalid Telegram username/link. Send @username or https://t.me/name.",reply_markup=back_admin()); return True
        entries=json.loads(get_setting("official_support") or "[]")
        entries.append({"name":context.user_data.get("support_new_name","Support"),"link":link})
        set_setting("official_support",json.dumps(entries)); clear_state(context)
        await update.message.reply_text("✅ Official support contact saved. It is now visible to users in Official Support.",reply_markup=admin_keyboard()); return True
    if state == "payout_channel":
        if text.upper()=="OFF":
            set_setting("payout_channel","")
        elif text.startswith("@"):
            set_setting("payout_channel",text)
        elif text.startswith("-100") or text.isdigit():
            set_setting("payout_channel",text)
        else:
            await update.message.reply_text("Send a valid channel ID (usually starts -100), @channelusername, or OFF.",reply_markup=back_admin()); return True
        clear_state(context); await update.message.reply_text("✅ Payout channel setting saved. Make sure the bot is an admin in that channel.",reply_markup=admin_keyboard()); return True
    if state == "menu_rename":
        key=context.user_data.get("menu_edit_key")
        labels=json.loads(get_setting("menu_labels") or "{}")
        labels[key]=text
        set_setting("menu_labels",json.dumps(labels)); clear_state(context)
        await update.message.reply_text("✅ Button renamed. The updated label will appear after the next menu refresh.",reply_markup=admin_keyboard()); return True

    if state == "set_bot_fund":
        value = parse_amount(text)
        if value is None:
            await update.message.reply_text("❌ Enter a valid amount.", reply_markup=back_admin())
            return True
        set_setting("bot_fund", str(value))
        if value > 0:
            set_setting("withdraw_enabled", "1")
        else:
            set_setting("withdraw_enabled", "0")
        clear_state(context)
        await update.message.reply_text(f"✅ Bot fund set to {money(value)}.\nAutomatic withdrawals: {'ON' if value > 0 else 'OFF'}.", reply_markup=admin_keyboard())
        return True

    if state == "maintenance_until":
        set_setting("maintenance_until", text)
        set_setting("maintenance", "1")
        clear_state(context)
        await update.message.reply_text(f"🛠 Maintenance ON for users. Expected end: {text}", reply_markup=admin_keyboard())
        return True

    if state == "set_channel_link":
        # Legacy compatibility: accept comma-separated links or OFF.
        if text.upper() == "OFF":
            db.execute("DELETE FROM settings WHERE key='required_channels'")
            db.commit()
            clear_state(context)
            await update.message.reply_text("✅ Forced channel subscription disabled.", reply_markup=admin_keyboard())
            return True
        links = [x.strip() for x in text.split(",") if x.strip()]
        if any(not (x.startswith("https://t.me/") or x.startswith("http://t.me/") or x.startswith("@")) for x in links):
            await update.message.reply_text("❌ Send valid channel links such as https://t.me/channelname.", reply_markup=back_admin())
            return True
        existing = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
        for link in links:
            if link not in existing:
                existing.append(link)
        set_setting("required_channels", ",".join(existing))
        clear_state(context)
        await update.message.reply_text("✅ Channel links saved successfully.", reply_markup=admin_keyboard())
        return True

    if state == "set_channel_add_link":
        link = text.strip()
        if not (link.startswith("https://t.me/") or link.startswith("http://t.me/") or link.startswith("@")):
            await update.message.reply_text("❌ Invalid channel link. Send a link like https://t.me/channelname.", reply_markup=back_admin())
            return True
        existing = [x.strip() for x in (get_setting("required_channels") or "").split(",") if x.strip()]
        if link in existing:
            await update.message.reply_text("ℹ️ This channel is already added. Add another link or use DONE.")
            clear_state(context)
            await admin_callback_refresh_channels_for_message(update, context)
            return True
        existing.append(link)
        set_setting("required_channels", ",".join(existing))
        clear_state(context)
        await update.message.reply_text(f"✅ Channel added successfully!\nTotal channels: {len(existing)}")
        await admin_callback_refresh_channels_for_message(update, context)
        return True

    if state == "task_name":

        context.user_data["task_name"] = text
        context.user_data["admin_state"] = "task_amount"

        await update.message.reply_text(
            "Step 2/3\n\n"
            "💰 Enter reward amount:",
            reply_markup=back_admin()
        )

        return True

    if state == "task_amount":

        value = parse_amount(text)

        if value is None:

            await update.message.reply_text(
                "❌ Invalid amount.",
                reply_markup=back_admin()
            )

            return True

        context.user_data["task_amount"] = value
        context.user_data["admin_state"] = "task_link"

        await update.message.reply_text(
            "Step 3/3\n\n"
            "🔗 Enter task link:",
            reply_markup=back_admin()
        )

        return True

    if state == "task_link":

        if not text.startswith(
            ("http://", "https://", "tg://")
        ):

            await update.message.reply_text(
                "❌ Invalid link.",
                reply_markup=back_admin()
            )

            return True

        db.execute("""
            INSERT INTO tasks(
                name,
                amount,
                link
            )
            VALUES(?,?,?)
        """, (
            context.user_data["task_name"],
            context.user_data["task_amount"],
            text
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ TASK ADDED!",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "gift_code":

        existing = db.execute("""
            SELECT 1
            FROM gifts
            WHERE code=?
        """, (text,)).fetchone()

        if existing:

            await update.message.reply_text(
                "❌ Gift code already exists.",
                reply_markup=back_admin()
            )

            return True

        context.user_data["gift_code"] = text
        context.user_data["admin_state"] = "gift_name"

        await update.message.reply_text(
            "Step 2/4\n\n"
            "Enter gift name:",
            reply_markup=back_admin()
        )

        return True

    if state == "gift_name":

        context.user_data["gift_name"] = text
        context.user_data["admin_state"] = "gift_amount"

        await update.message.reply_text(
            "Step 3/4\n\n"
            "💰 Enter reward amount:",
            reply_markup=back_admin()
        )

        return True

    if state == "gift_amount":

        value = parse_amount(text)

        if value is None:

            await update.message.reply_text(
                "❌ Invalid amount.",
                reply_markup=back_admin()
            )

            return True

        context.user_data["gift_amount"] = value
        context.user_data["admin_state"] = "gift_limit"

        await update.message.reply_text(
            "Step 4/5\n\n"
            "👤 Per-account use limit?\n\n"
            "Example: 1",
            reply_markup=back_admin()
        )

        return True

    if state == "gift_limit":
        try:
            limit = int(text)
            if limit < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a positive number, e.g. 1.", reply_markup=back_admin())
            return True
        context.user_data["gift_per_account"] = limit
        context.user_data["admin_state"] = "gift_total_limit"
        await update.message.reply_text("Step 5/5\n\n👥 Total number of users who can claim this code?\nExample: 4", reply_markup=back_admin())
        return True

    if state == "gift_total_limit":
        try:
            total_limit = int(text)
            if total_limit < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a positive number, e.g. 4.", reply_markup=back_admin())
            return True
        db.execute("""
            INSERT INTO gifts(code, name, amount, per_account, total_limit)
            VALUES(?,?,?,?,?)
        """, (context.user_data["gift_code"], context.user_data["gift_name"],
              context.user_data["gift_amount"], context.user_data["gift_per_account"], total_limit))
        db.commit()
        clear_state(context)
        await update.message.reply_text(f"✅ GIFT CREATED!\nTotal claim limit: {total_limit} users.", reply_markup=admin_keyboard())
        return True

    if state == "balance_user":

        try:

            uid = int(text)

        except:

            await update.message.reply_text(
                "❌ User ID must be a number.",
                reply_markup=back_admin()
            )

            return True

        if not get_user(uid):

            await update.message.reply_text(
                "❌ User not found.",
                reply_markup=back_admin()
            )

            return True

        context.user_data["balance_user"] = uid
        context.user_data["admin_state"] = "balance_action"

        await update.message.reply_text(
            "Send:\n\n"
            "ADD 100\n\n"
            "or\n\n"
            "DECREASE 100",
            reply_markup=back_admin()
        )

        return True

    if state == "balance_action":

        parts = text.split()

        if len(parts) != 2:

            await update.message.reply_text(
                "❌ Example:\n"
                "ADD 100\n"
                "DECREASE 100",
                reply_markup=back_admin()
            )

            return True

        action = parts[0].upper()
        value = parse_amount(parts[1])

        if action not in [
            "ADD",
            "DECREASE"
        ] or value is None:

            await update.message.reply_text(
                "❌ Invalid format.",
                reply_markup=back_admin()
            )

            return True

        uid = context.user_data["balance_user"]
        user = get_user(uid)

        if action == "DECREASE":

            if user["balance"] < value:

                await update.message.reply_text(
                    "❌ Not enough balance.",
                    reply_markup=back_admin()
                )

                return True

            db.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
            """, (
                value,
                uid
            ))

        else:

            db.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                value,
                uid
            ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            f"💰 Admin (Decrease or increase) In your Balance By {money(value)}\n\n✅ USER BALANCE UPDATED.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_gift_name":

        gift_id = context.user_data["gift_id"]

        db.execute("""
            UPDATE gifts
            SET name=?
            WHERE id=?
        """, (
            text,
            gift_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Gift name updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_gift_amount":

        value = parse_amount(text)

        if value is None:

            await update.message.reply_text(
                "❌ Invalid amount.",
                reply_markup=back_admin()
            )

            return True

        gift_id = context.user_data["gift_id"]

        db.execute("""
            UPDATE gifts
            SET amount=?
            WHERE id=?
        """, (
            value,
            gift_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Gift amount updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_gift_limit":

        try:

            limit = int(text)

            if limit < 1:
                raise ValueError

        except:

            await update.message.reply_text(
                "❌ Invalid limit.",
                reply_markup=back_admin()
            )

            return True

        gift_id = context.user_data["gift_id"]

        db.execute("""
            UPDATE gifts
            SET per_account=?
            WHERE id=?
        """, (
            limit,
            gift_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Gift limit updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_task_name":

        task_id = context.user_data["task_id"]

        db.execute("""
            UPDATE tasks
            SET name=?
            WHERE id=?
        """, (
            text,
            task_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Task name updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_task_amount":

        value = parse_amount(text)

        if value is None:

            await update.message.reply_text(
                "❌ Invalid amount.",
                reply_markup=back_admin()
            )

            return True

        task_id = context.user_data["task_id"]

        db.execute("""
            UPDATE tasks
            SET amount=?
            WHERE id=?
        """, (
            value,
            task_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Task reward updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    if state == "edit_task_link":

        if not text.startswith(
            ("http://", "https://", "tg://")
        ):

            await update.message.reply_text(
                "❌ Invalid link.",
                reply_markup=back_admin()
            )

            return True

        task_id = context.user_data["task_id"]

        db.execute("""
            UPDATE tasks
            SET link=?
            WHERE id=?
        """, (
            text,
            task_id
        ))

        db.commit()
        clear_state(context)

        await update.message.reply_text(
            "✅ Task link updated.",
            reply_markup=main_menu(
                update.effective_user.id
            )
        )

        return True

    return False


# ============================================================
# PHOTO HANDLER
# ============================================================

async def broadcast_media_handler(update, context):
    msg=update.message
    if not msg or not is_admin(update.effective_user.id): return
    state=context.user_data.get("admin_state")
    if state not in ("user_broadcast_content", "broadcast_message"): return
    if msg.text or msg.caption: return  # handled by text router for text/caption content
    if state == "user_broadcast_content":
        target=context.user_data.get("broadcast_target_id")
        clear_state(context)
        try:
            await msg.copy(chat_id=target)
            await msg.reply_text(f"✅ Message sent to user {target}.",reply_markup=admin_keyboard())
        except Exception:
            await msg.reply_text("❌ Could not send message to that user.",reply_markup=admin_keyboard())
        return
    clear_state(context)
    rows=db.execute("SELECT user_id FROM users").fetchall(); sent=failed=0
    for row in rows:
        try: await msg.copy(chat_id=row["user_id"]); sent+=1
        except Exception as exc: logging.warning("Media broadcast failed: %s",exc); failed+=1
    await msg.reply_text(f"📢 BOT BROADCAST FINISHED\n\nDelivered: {sent}\nFailed: {failed}",reply_markup=admin_keyboard())


async def photo_handler(update, context):

    if not await check_forced_sub(update, context):
        return await start(update, context)

    state = context.user_data.get(
        "state"
    )

    if state != "task_screenshot":

        return

    if not update.message.photo:

        return

    photo = update.message.photo[-1]

    await submit_task_request(

        update,
        context,

        request_type="SCREENSHOT",

        screenshot=photo.file_id
    )


# ============================================================
# NORMAL TEXT ROUTER
# ============================================================

async def text_router(update, context):

    if get_setting("maintenance") == "1" and not is_admin(update.effective_user.id):
        await update.message.reply_text(f"🛠 Currently bot maintenance.\nExpected end: {get_setting('maintenance_until') or 'until further notice'}")
        return

    if not await check_forced_sub(update, context):
        return await start(update, context)

    save_user(update.effective_user)

    text = update.message.text.strip()
    try: labels=json.loads(get_setting("menu_labels") or "{}")
    except Exception: labels={}
    try: colors=json.loads(get_setting("menu_colors") or "{}")
    except Exception: colors={}
    normalized=text
    if normalized.startswith(("🟢 ", "🔴 ", "🔵 ")): normalized=normalized[2:]
    key_by_label={labels.get(k, MENU_LABELS.get(k,k)):k for k in MENU_LABELS}
    key=key_by_label.get(normalized)
    if key:
        routes={"BOT TASK":"📋 BOT TASK","MY BALANCE":"💸 MY BALANCE","GIFT CODE":"🎁 GIFT CODE","QUICK PAY":"⚡ QUICK PAY","WITHDRAW":"🚀 WITHDRAW","PAYOUT METHOD":"💳 PAYOUT METHOD","OFFICIAL SUPPORT":"🆘 Official Support"}
        text=routes.get(key,text)

    if text == "📋 BOT TASK":

        clear_state(context)

        await show_tasks_message(
            update,
            context
        )

        return

    if text == "💸 MY BALANCE":

        await my_balance(
            update,
            context
        )

        return

    if text == "🎁 GIFT CODE":

        await gift_menu(
            update,
            context
        )

        return

    if text == "⚡ QUICK PAY":

        await quick_pay(
            update,
            context
        )

        return

    if text == "💳 PAYOUT METHOD":

        await payout_method(
            update,
            context
        )

        return

    if text == "🚀 WITHDRAW":

        await withdraw_menu(
            update,
            context
        )

        return

    if text == "🆘 Official Support":
        raw = get_setting("official_support") or "[]"
        try:
            entries = json.loads(raw)
        except Exception:
            entries = []
        if not entries:
            await update.message.reply_text("🆘 Official Support\n\nNo support contacts have been added yet.", reply_markup=main_menu(update.effective_user.id))
        else:
            rows = [[InlineKeyboardButton(str(item.get("name", "Support")), url=str(item.get("link", "https://t.me/")))] for item in entries if item.get("name") and item.get("link")]
            await update.message.reply_text("🆘 OFFICIAL SUPPORT\n\nChoose a contact below:", reply_markup=InlineKeyboardMarkup(rows) if rows else main_menu(update.effective_user.id))
        return

    if context.user_data.get(
        "admin_state"
    ):

        handled = await admin_text(
            update,
            context
        )

        if handled:

            return

    state = context.user_data.get(
        "state"
    )

    if state == "gift":

        await process_gift(
            update,
            context
        )

        return

    if state == "upi":

        await save_upi(
            update,
            context
        )

        return

    if state == "send_user":

        await quick_pay_user(
            update,
            context
        )

        return

    if state == "send_amount":

        await quick_pay_amount(
            update,
            context
        )

        return

    if state == "gateway_setup_id":
        payment_id=text.strip()
        if not payment_id.isdigit() or len(payment_id)!=10:
            await update.message.reply_text("❌ Gateway ID must be exactly 10 digits. Try again:",reply_markup=back_main()); return
        name=context.user_data.get("gateway_setup_name")
        saved=json.loads(get_setting(f"gateway_ids_{update.effective_user.id}") or "{}")
        saved[name]=payment_id
        set_setting(f"gateway_ids_{update.effective_user.id}",json.dumps(saved))
        context.user_data.pop("state",None); context.user_data.pop("gateway_setup_name",None)
        await update.message.reply_text(f"✅ {name} payment ID saved.",reply_markup=main_menu(update.effective_user.id)); return

    if state == "gateway_payment_id":
        digits=text.strip()
        if not digits.isdigit() or len(digits)!=10:
            await update.message.reply_text("❌ Enter a valid 10-digit ID.",reply_markup=back_main()); return
        context.user_data["withdraw_payment"]=digits; context.user_data["state"]="withdraw"
        await update.message.reply_text(f"ID saved: {digits}\\n\\nEnter withdrawal amount:",reply_markup=back_main()); return

    if state == "withdraw":
        await create_withdrawal(update, context)
        return

    if state == "task_refer":

        await submit_task_request(

            update,
            context,

            request_type="REFER_LINK_OR_NUMBER",

            submission=text
        )

        return

    await update.message.reply_text(

        "Please select an option:",

        reply_markup=main_menu(
            update.effective_user.id
        )
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context
):

    query = update.callback_query
    data = query.data

    if data in {
        "wallet_refresh", "wallet_fund",
        "wallet_earning", "wallet_withdrawal",
        "wallet_earning_all", "wallet_withdrawal_all",
    }:
        await query.answer()
        if not await check_forced_sub(update, context):
            await query.message.reply_text("Please join the required channels first.")
            return

        uid = query.from_user.id

        if data in {"wallet_refresh", "wallet_fund"}:
            user = get_user(uid)
            fund = float(get_setting("bot_fund") or 0)
            if data == "wallet_fund":
                await query.message.reply_text(
                    f"💰 BOT FUND / LIVE FUND\n\nAvailable bot fund: {money(fund)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ WALLET", callback_data="wallet_refresh")],
                        [InlineKeyboardButton("🏠 MAIN MENU", callback_data="back_main")],
                    ]),
                )
                return

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 EARNING HISTORY", callback_data="wallet_earning"),
                    InlineKeyboardButton("🧾 WITHDRAWAL HISTORY", callback_data="wallet_withdrawal"),
                ],
                [InlineKeyboardButton("💰 BOT FUND / LIVE FUND", callback_data="wallet_fund")],
                [InlineKeyboardButton("🔄 REFRESH", callback_data="wallet_refresh")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="back_main")],
            ])
            await query.message.reply_text(
                "━━━━━━━━━━━━━━━━━━\n💳 WALLET OVERVIEW\n━━━━━━━━━━━━━━━━━━\n\n"
                f"🔵 WALLET ID → {uid}\n🧾 BALANCE → {money(user['balance'])}\n"
                f"👤 TOTAL USER → {db.execute('SELECT COUNT(*) FROM users').fetchone()[0]}\n\n"
                "Built with security you can trust.\n"
                "Support that responds promptly.",
                reply_markup=keyboard,
            )
            return

        show_all = data.endswith("_all")
        is_earning = data.startswith("wallet_earning")
        if is_earning:
            sql = """
                SELECT r.*, t.name AS task_name
                FROM task_requests r
                LEFT JOIN tasks t ON t.id=r.task_id
                WHERE r.user_id=? AND r.status='APPROVED'
                ORDER BY r.id DESC
            """
            if not show_all:
                sql += " LIMIT 3"
            rows = db.execute(sql, (uid,)).fetchall()
            title = "📊 EARNING HISTORY — APPROVED TASKS"
        else:
            sql = """
                SELECT amount, status, created_at, processed_at
                FROM withdrawals
                WHERE user_id=?
                ORDER BY id DESC
            """
            if not show_all:
                sql += " LIMIT 3"
            rows = db.execute(sql, (uid,)).fetchall()
            title = "🧾 WITHDRAWAL HISTORY"

        lines = [title, ""]
        if not rows:
            lines.append("No approved task earnings yet." if is_earning else "No withdrawal requests yet.")
        else:
            for r in rows:
                if is_earning:
                    lines.extend([
                        f"✅ Task: {r['task_name'] or 'Task'}",
                        f"💰 Amount: {money(r['reward'])}",
                        f"🕒 Time: {r['processed_at'] or r['created_at']}",
                        "━━━━━━━━━━━━━━━━━━",
                    ])
                else:
                    lines.extend([
                        f"💰 Amount: {money(r['amount'])}",
                        f"📌 Status: {r['status']}",
                        f"🕒 Time: {r['processed_at'] or r['created_at']}",
                        "━━━━━━━━━━━━━━━━━━",
                    ])

        if not show_all:
            all_count = db.execute(
                "SELECT COUNT(*) FROM task_requests WHERE user_id=? AND status='APPROVED'"
                if is_earning else
                "SELECT COUNT(*) FROM withdrawals WHERE user_id=?",
                (uid,),
            ).fetchone()[0]
            all_callback = "wallet_earning_all" if is_earning else "wallet_withdrawal_all"
            buttons = [
                [InlineKeyboardButton("👁️ VIEW ALL HISTORY", callback_data=all_callback)],
                [InlineKeyboardButton("⬅️ WALLET", callback_data="wallet_refresh")],
                [InlineKeyboardButton("🏠 MAIN MENU", callback_data="back_main")],
            ]
        else:
            buttons = [
                [InlineKeyboardButton("⬅️ WALLET", callback_data="wallet_refresh")],
                [InlineKeyboardButton("🏠 MAIN MENU", callback_data="back_main")],
            ]

        full_text = "\n".join(lines)
        if len(full_text) <= 3900:
            await query.message.reply_text(
                full_text,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            # Telegram messages have a length limit. Send the full history in safe-sized chunks.
            chunks = []
            current = title + "\n\n"
            for line in lines[2:]:
                if len(current) + len(line) + 1 > 3500:
                    chunks.append(current)
                    current = title + " (continued)\n\n"
                current += line + "\n"
            if current.strip():
                chunks.append(current)
            for index, chunk in enumerate(chunks):
                markup = InlineKeyboardMarkup(buttons) if index == len(chunks) - 1 else None
                await query.message.reply_text(chunk, reply_markup=markup)
        return

    if data == "check_join":
        await check_join_callback(update, context)
        return

    if data == "nothing":

        await query.answer()

        return

    if data == "back_main":

        await query.answer()
        clear_state(context)

        await query.message.reply_text(
            "🏠 MAIN MENU",
            reply_markup=main_menu(
                query.from_user.id
            )
        )

        return

    if data == "back_tasks":

        await query.answer()
        clear_state(context)

        tasks = db.execute("""
            SELECT *
            FROM tasks
            WHERE active=1
            ORDER BY id DESC
        """).fetchall()

        keyboard = []

        for task in tasks:

            keyboard.append([
                InlineKeyboardButton(
                    f"{task['name']} ⚡ ({money(task['amount'])})",
                    callback_data=f"task:{task['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_main"
            )
        ])

        await query.message.reply_text(
            "📋 AVAILABLE TASKS",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    if data == "back_admin":

        await query.answer()
        clear_state(context)

        await query.message.reply_text(
            "🛠 ADMIN CONTROL",
            reply_markup=admin_keyboard()
        )

        return

    if data == "admin_gifts":

        await query.answer()
        clear_state(context)

        gifts = db.execute("""
            SELECT *
            FROM gifts
            ORDER BY id DESC
        """).fetchall()

        keyboard = []

        for gift in gifts:

            keyboard.append([
                InlineKeyboardButton(
                    f"🎁 {gift['code']} • {money(gift['amount'])}",
                    callback_data=f"edit_gift:{gift['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "➕ ADD GIFT",
                callback_data="admin_add_gift"
            )
        ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="back_admin"
            )
        ])

        await query.message.reply_text(
            "🎁 GIFT MANAGEMENT",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    if data == "withdraw_confirm":
        await query.answer()
        await submit_confirmed_withdrawal(update, context)
        return
    if data == "withdraw_cancel":
        clear_state(context)
        try: await query.message.edit_text("❌ Withdrawal cancelled.")
        except Exception: pass
        await query.answer("Cancelled.")
        return

    if data.startswith("withdraw_method:"):
        method=data.split(":",1)[1]; context.user_data["withdraw_method"]=method
        if method=="UPI":
            u=get_user(query.from_user.id)
            if not u or not u["upi"]:
                await query.answer("Set UPI first.",show_alert=True); await set_upi(update,context); return
            context.user_data["withdraw_payment"]=u["upi"]; context.user_data["state"]="withdraw"
            await query.answer(); await query.message.reply_text(f"UPI selected\n{u['upi']}\n\nEnter withdrawal amount:",reply_markup=back_main())
        else:
            saved=json.loads(get_setting(f"gateway_ids_{query.from_user.id}") or "{}")
            payment=saved.get(method)
            if not payment:
                await query.answer("Set gateway ID in PAYMENT METHOD first.",show_alert=True); await query.message.reply_text(f"⚠️ {method} is not set. Open PAYMENT METHOD and save your ID first.",reply_markup=back_main()); return
            context.user_data["withdraw_payment"]=payment; context.user_data["state"]="withdraw"
            await query.answer(); await query.message.reply_text(f"{method} selected\nID: {payment}\n\nEnter withdrawal amount:",reply_markup=back_main())
        return
    if data.startswith("set_gateway_id:"):
        name=data.split(":",1)[1]; context.user_data["gateway_setup_name"]=name; context.user_data["state"]="gateway_setup_id"
        await query.answer(); await query.message.reply_text(f"Set {name} payment ID (10 digits):",reply_markup=back_main()); return
    if data == "noop":
        await query.answer(); return

    if data == "setupi":

        await set_upi(
            update,
            context
        )

        return

    if data.startswith("task:"):

        await task_details(
            update,
            context
        )

        return

    if data.startswith("upload:"):

        await upload_task(
            update,
            context
        )

        return

    if data.startswith("refer:"):

        await refer_task(
            update,
            context
        )

        return

    if data.startswith(
        "taskapprove:"
    ) or data.startswith(
        "taskreject:"
    ):

        await process_task_request(
            update,
            context
        )

        return

    if data.startswith(
        "approve:"
    ) or data.startswith(
        "reject:"
    ):

        await process_withdrawal(
            update,
            context
        )

        return

    if data.startswith(
        "admin_"
    ) or data.startswith(
        "menu_"
    ) or data.startswith(
        "edit_gift:"
    ) or data.startswith(
        "giftname:"
    ) or data.startswith(
        "giftamount:"
    ) or data.startswith(
        "giftlimit:"
    ) or data.startswith(
        "gifttoggle:"
    ) or data.startswith(
        "giftdel:"
    ) or data.startswith(
        "edit_task:"
    ) or data.startswith(
        "taskeditname:"
    ) or data.startswith(
        "taskeditamount:"
    ) or data.startswith(
        "taskeditlink:"
    ) or data.startswith(
        "tasktoggle:"
    ) or data.startswith(
        "taskdel:"
    ):

        await admin_callback(
            update,
            context
        )

        return

    await query.answer()


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    logging.error(
        "BOT ERROR: %s",
        context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    init_db()

    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "id",
            my_id
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_control
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    app.add_handler(MessageHandler(filters.ALL, broadcast_media_handler))

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "================================"
    )
    print(
        "ERAN TASK BOT STARTED"
    )
    print(
        "================================"
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
