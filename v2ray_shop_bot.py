"""
✨ V2RAY SHOP BOT
Final Single-File Production Edition

A Telegram V2Ray shop bot built around aiogram 3 + aiohttp + PostgreSQL (psycopg).
Designed for Render Web Service + Render PostgreSQL:
- Binds to 0.0.0.0:$PORT
- Exposes /health and /ready
- Uses Telegram webhook
- Uses PostgreSQL for persistent production data
- Includes shop, inventory, cart, orders, wallet, coupons,
  referrals, tickets, favorites, broadcast, admin dashboard,
  audit logs, backups and a simple REST API.

Environment variables:
BOT_TOKEN                 Telegram bot token (required)
ADMIN_IDS                 Comma-separated Telegram user IDs (required)
WEBHOOK_BASE_URL          Render public URL, e.g. https://your-app.onrender.com
WEBHOOK_SECRET            Random secret for webhook path
DATABASE_URL              PostgreSQL connection URL (required)
SHOP_NAME                 Default: ✨ V2RAY SHOP BOT
CURRENCY                  Default: تومان
CARD_NUMBER               Manual payment card number (optional)
CARD_OWNER                Manual payment owner name (optional)
REFERRAL_REWARD           Referral reward after first completed purchase (default: 20000)
PORT                      Render supplies this automatically; default 10000
API_KEY                   Required for protected REST stats endpoint
SEED_DEMO                 0 by default; set 1 only for local/demo testing
LOG_LEVEL                 INFO by default

Install:
pip install aiogram aiohttp psycopg[binary]

Run:
python v2ray_shop_bot.py
"""

import asyncio
import json
import logging
import os
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
import psycopg
from psycopg.rows import dict_row
from psycopg import errors as pg_errors
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOP_NAME = os.getenv("SHOP_NAME", "✨ V2RAY SHOP BOT")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)
CURRENCY = os.getenv("CURRENCY", "تومان")
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "").strip()
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "20000"))
PORT = int(os.getenv("PORT", "10000"))
API_KEY = os.getenv("API_KEY", "").strip()
SEED_DEMO = os.getenv("SEED_DEMO", "0").strip().lower() in ("1", "true", "yes", "on")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("v2ray-shop-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS environment variable is required and must contain at least one Telegram user ID.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(value: int) -> str:
    return f"{int(value):,} {CURRENCY}"


def human_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    return f"{value / 1024:.1f} KB"


def make_code(prefix: str = "VSB") -> str:
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "-" + "".join(secrets.choice(alphabet) for _ in range(10))


def esc(value) -> str:
    # Minimal Telegram HTML escaping.
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT 'NORMAL',
    is_banned INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,
    referral_rewarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    category_id INTEGER,
    name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    traffic_gb INTEGER NOT NULL DEFAULT 0,
    duration_days INTEGER NOT NULL DEFAULT 30,
    devices INTEGER NOT NULL DEFAULT 1,
    price INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    featured INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS configs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    product_id INTEGER NOT NULL,
    config TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    reserved_by INTEGER,
    reserved_until TEXT,
    sold_to INTEGER,
    sold_order_id INTEGER,
    sold_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cart_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, product_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'percent',
    value INTEGER NOT NULL,
    min_order INTEGER NOT NULL DEFAULT 0,
    max_discount INTEGER,
    usage_limit INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    per_user_limit INTEGER NOT NULL DEFAULT 1,
    starts_at TEXT,
    ends_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS coupon_uses (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    coupon_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    used_at TEXT NOT NULL,
    FOREIGN KEY(coupon_id) REFERENCES coupons(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    order_code TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    subtotal INTEGER NOT NULL,
    discount INTEGER NOT NULL DEFAULT 0,
    wallet_used INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT',
    payment_method TEXT,
    payment_ref TEXT,
    coupon_id INTEGER,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT NOT NULL,
    reference TEXT,
    receipt_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by BIGINT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    balance_before INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reference_id TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    invited_id INTEGER UNIQUE NOT NULL,
    rewarded INTEGER NOT NULL DEFAULT 0,
    reward_amount INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(inviter_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(invited_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    priority TEXT NOT NULL DEFAULT 'NORMAL',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, product_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, dsn: str):
        if not dsn:
            raise RuntimeError("DATABASE_URL environment variable is required for PostgreSQL.")
        self.dsn = dsn

    async def connect(self):
        return await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _sql(query: str) -> str:
        return query

    async def init(self):
        async with await self.connect() as db:
            await db.execute(SCHEMA)
            await db.commit()
        await self.migrate_legacy_schema()
        await self.seed()

    async def migrate_legacy_schema(self):
        # Upgrade installations created by earlier builds without deleting data.
        statements = [
            "ALTER TABLE users ALTER COLUMN telegram_id TYPE BIGINT",
            "ALTER TABLE admin_logs ALTER COLUMN admin_id TYPE BIGINT",
            "ALTER TABLE payments ALTER COLUMN reviewed_by TYPE BIGINT",
        ]
        async with await self.connect() as conn:
            for statement in statements:
                try:
                    await conn.execute(statement)
                except pg_errors.UndefinedColumn:
                    pass
            await conn.commit()

    async def seed(self):
        async with await self.connect() as db:
            cur = await db.execute("SELECT COUNT(*) AS c FROM categories")
            count = (await cur.fetchone())["c"]
            if count == 0:
                await db.executemany(
                    "INSERT INTO categories(name,emoji,sort_order) VALUES(%s,%s,%s)",
                    [
                        ("VLESS", "🔐", 1),
                        ("VMess", "⚡", 2),
                        ("Trojan", "🛡️", 3),
                        ("Shadowsocks", "🚀", 4),
                    ],
                )

            cur = await db.execute("SELECT COUNT(*) AS c FROM products")
            pcount = (await cur.fetchone())["c"]
            if pcount == 0:
                cats = {}
                cur = await db.execute("SELECT id,name FROM categories")
                async for row in cur:
                    cats[row["name"]] = row["id"]

                products = [
                    (cats["VLESS"], "VLESS 30GB", "VLESS", "سرویس پایدار و سریع", 30, 30, 2, 250000, 1, 1),
                    (cats["VLESS"], "VLESS 50GB", "VLESS", "سرویس اقتصادی", 50, 30, 3, 350000, 1, 0),
                    (cats["VMess"], "VMess 30GB", "VMess", "مناسب استفاده روزمره", 30, 30, 2, 230000, 1, 0),
                    (cats["Trojan"], "Trojan 50GB", "Trojan", "امن و سریع", 50, 30, 3, 390000, 1, 1),
                    (cats["Shadowsocks"], "SS 20GB", "Shadowsocks", "ساده و سبک", 20, 30, 2, 180000, 1, 0),
                ]
                await db.executemany(
                    """INSERT INTO products
                    (category_id,name,protocol,description,traffic_gb,duration_days,devices,price,active,featured,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [x + (now_iso(),) for x in products],
                )

                if SEED_DEMO:
                    cur = await db.execute("SELECT id FROM products")
                    product_ids = [r["id"] async for r in cur]
                    for pid in product_ids:
                        demo_configs = [
                            f"vless://DEMO-CONFIG-{pid}-{i}@example.com:443?security=tls&type=ws#V2RAYSHOP-{pid}-{i}"
                            for i in range(1, 4)
                        ]
                        await db.executemany(
                            """INSERT INTO configs(product_id,config,status,created_at)
                               VALUES(%s,%s,%s,%s)""",
                            [(pid, cfg, "AVAILABLE", now_iso()) for cfg in demo_configs],
                        )

            defaults = {
                "shop_name": SHOP_NAME,
                "currency": CURRENCY,
                "referral_reward": str(REFERRAL_REWARD),
            }
            for k, v in defaults.items():
                await db.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING", (k, v))
            await db.commit()

    async def one(self, query, params=()):
        async with await self.connect() as db:
            cur = await db.execute(query, params)
            return await cur.fetchone()

    async def all(self, query, params=()):
        async with await self.connect() as db:
            cur = await db.execute(query, params)
            return await cur.fetchall()

    async def run(self, query, params=()):
        async with await self.connect() as db:
            q = query.strip().rstrip(";")
            if q.upper().startswith("INSERT INTO") and "RETURNING" not in q.upper():
                table = q.split()[2].split("(")[0].strip().lower()
                if table not in {"settings"}:
                    q += " RETURNING id"
            cur = await db.execute(q, params)
            result = None
            if "RETURNING" in q.upper():
                row = await cur.fetchone()
                result = row["id"] if row and "id" in row else None
            await db.commit()
            return result

db = DB(DATABASE_URL)


# ---------------------------------------------------------------------------
# User / wallet / audit
# ---------------------------------------------------------------------------

async def get_or_create_user(tg_user, referral_code: Optional[str] = None):
    row = await db.one("SELECT * FROM users WHERE telegram_id=%s", (tg_user.id,))
    if row:
        await db.run(
            """UPDATE users SET username=%s,first_name=%s,last_name=%s,updated_at=%s
               WHERE telegram_id=%s""",
            (tg_user.username, tg_user.first_name, tg_user.last_name, now_iso(), tg_user.id),
        )
        return await db.one("SELECT * FROM users WHERE telegram_id=%s", (tg_user.id,))

    referred_by = None
    if referral_code and referral_code.startswith("ref_"):
        try:
            inviter = int(referral_code[4:])
            if inviter != tg_user.id:
                inv = await db.one("SELECT id FROM users WHERE telegram_id=%s", (inviter,))
                if inv:
                    referred_by = inv["id"]
        except ValueError:
            pass

    try:
        uid = await db.run(
            """INSERT INTO users
            (telegram_id,username,first_name,last_name,referred_by,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                tg_user.id,
                tg_user.username,
                tg_user.first_name,
                tg_user.last_name,
                referred_by,
                now_iso(),
                now_iso(),
            ),
        )
    except pg_errors.UniqueViolation:
        existing = await db.one("SELECT * FROM users WHERE telegram_id=%s", (tg_user.id,))
        if existing:
            return existing
        raise
    if referred_by:
        await db.run(
            """INSERT INTO referrals(inviter_id,invited_id,created_at)
               VALUES(%s,%s,%s) ON CONFLICT(invited_id) DO NOTHING""",
            (referred_by, uid, now_iso()),
        )
    return await db.one("SELECT * FROM users WHERE id=%s", (uid,))


async def audit(admin_id: int, action: str, target="", details=""):
    await db.run(
        """INSERT INTO admin_logs(admin_id,action,target,details,created_at)
           VALUES(%s,%s,%s,%s,%s)""",
        (admin_id, action, str(target), str(details), now_iso()),
    )


async def wallet_add(user_id: int, amount: int, kind: str, reference_id="", note=""):
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    async with await db.connect() as conn:
        await conn.execute("BEGIN")
        row = await (await conn.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (user_id,))).fetchone()
        if not row:
            raise ValueError("User not found.")
        before = row["balance"]
        after = before + amount
        await conn.execute("UPDATE users SET balance=%s,updated_at=%s WHERE id=%s", (after, now_iso(), user_id))
        await conn.execute(
            """INSERT INTO wallet_transactions
            (user_id,amount,balance_before,balance_after,kind,reference_id,note,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, amount, before, after, kind, reference_id, note, now_iso()),
        )
        await conn.commit()
        return after


async def wallet_subtract(user_id: int, amount: int, kind: str, reference_id="", note=""):
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    async with await db.connect() as conn:
        await conn.execute("BEGIN")
        row = await (await conn.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (user_id,))).fetchone()
        if not row or row["balance"] < amount:
            raise ValueError("Insufficient wallet balance.")
        before = row["balance"]
        after = before - amount
        await conn.execute("UPDATE users SET balance=%s,updated_at=%s WHERE id=%s", (after, now_iso(), user_id))
        await conn.execute(
            """INSERT INTO wallet_transactions
            (user_id,amount,balance_before,balance_after,kind,reference_id,note,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, -amount, before, after, kind, reference_id, note, now_iso()),
        )
        await conn.commit()
        return after


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_kb(admin=False):
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🛍 خرید سرویس", callback_data="shop"),
        InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="orders"),
    )
    b.row(
        InlineKeyboardButton(text="🛒 سبد خرید", callback_data="cart"),
        InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet"),
    )
    b.row(
        InlineKeyboardButton(text="🎁 دعوت دوستان", callback_data="referral"),
        InlineKeyboardButton(text="🎟 کد تخفیف", callback_data="coupon"),
    )
    b.row(
        InlineKeyboardButton(text="❤️ علاقه‌مندی‌ها", callback_data="favorites"),
        InlineKeyboardButton(text="🆘 پشتیبانی", callback_data="support"),
    )
    b.row(
        InlineKeyboardButton(text="👤 حساب کاربری", callback_data="profile"),
        InlineKeyboardButton(text="📚 راهنما", callback_data="help"),
    )
    if admin:
        b.row(InlineKeyboardButton(text="👑 پنل مدیریت", callback_data="admin"))
    return b.as_markup()


def back_home():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")]]
    )


def shop_kb(categories):
    b = InlineKeyboardBuilder()
    for c in categories:
        b.button(text=f'{c["emoji"]} {c["name"]}', callback_data=f'cat:{c["id"]}')
    b.adjust(2)
    b.row(InlineKeyboardButton(text="🔥 پیشنهادهای ویژه", callback_data="featured"))
    b.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home"))
    return b.as_markup()


def products_kb(products):
    b = InlineKeyboardBuilder()
    for p in products:
        stock = "🟢" if p["stock"] > 0 else "🔴"
        b.row(
            InlineKeyboardButton(
                text=f'{stock} {p["name"]} — {money(p["price"])}',
                callback_data=f'product:{p["id"]}',
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ دسته‌ها", callback_data="shop"))
    return b.as_markup()


def product_detail_kb(product_id, favorite=False):
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="⚡ خرید فوری", callback_data=f"buy:{product_id}"),
        InlineKeyboardButton(text="🛒 افزودن به سبد", callback_data=f"addcart:{product_id}"),
    )
    b.row(
        InlineKeyboardButton(
            text="💔 حذف از علاقه‌مندی" if favorite else "❤️ علاقه‌مندی",
            callback_data=f"fav:{product_id}",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ بازگشت", callback_data="shop"))
    return b.as_markup()


def admin_kb():
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📊 داشبورد", callback_data="adm:dashboard"),
        InlineKeyboardButton(text="🧾 سفارش‌ها", callback_data="adm:orders"),
    )
    b.row(
        InlineKeyboardButton(text="💳 پرداخت‌ها", callback_data="adm:payments"),
        InlineKeyboardButton(text="📦 موجودی", callback_data="adm:inventory"),
    )
    b.row(
        InlineKeyboardButton(text="🛍 محصولات", callback_data="adm:products"),
        InlineKeyboardButton(text="👥 کاربران", callback_data="adm:users"),
    )
    b.row(
        InlineKeyboardButton(text="🎫 تیکت‌ها", callback_data="adm:tickets"),
        InlineKeyboardButton(text="📢 پیام همگانی", callback_data="adm:broadcast"),
    )
    b.row(
        InlineKeyboardButton(text="🎟 کوپن", callback_data="adm:coupons"),
        InlineKeyboardButton(text="💾 بکاپ", callback_data="adm:backup"),
    )
    b.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home"))
    return b.as_markup()


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class CouponState(StatesGroup):
    waiting_code = State()


class WalletState(StatesGroup):
    waiting_amount = State()


class TicketState(StatesGroup):
    waiting_subject = State()
    waiting_message = State()


class BroadcastState(StatesGroup):
    waiting_message = State()


class ConfigState(StatesGroup):
    waiting_product_id = State()
    waiting_config = State()


class AddProductState(StatesGroup):
    waiting_name = State()
    waiting_protocol = State()
    waiting_price = State()
    waiting_traffic = State()
    waiting_duration = State()
    waiting_devices = State()


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


async def render_home(user_id: int):
    user = await db.one("SELECT * FROM users WHERE telegram_id=%s", (user_id,))
    first = esc(user["first_name"] or "دوست")
    balance = money(user["balance"])
    admin = is_admin(user_id)
    text = (
        f"<b>{SHOP_NAME}</b>\n\n"
        f"سلام {first} 👋\n"
        f"به فروشگاه حرفه‌ای سرویس‌های V2Ray خوش آمدید.\n\n"
        f"💰 موجودی کیف پول: <b>{balance}</b>\n"
        f"🏆 سطح: <b>{esc(user['level'])}</b>\n\n"
        f"از منوی زیر انتخاب کنید:"
    )
    return text, main_kb(admin)


@dp.message(CommandStart())
async def start(message: Message):
    args = (message.text or "").split(maxsplit=1)
    referral = args[1] if len(args) > 1 else None
    user = await get_or_create_user(message.from_user, referral)
    if user["is_banned"]:
        await message.answer("⛔ حساب شما مسدود شده است.")
        return
    text, kb = await render_home(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("menu"))
async def menu(message: Message):
    await get_or_create_user(message.from_user)
    text, kb = await render_home(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.answer()
    text, kb = await render_home(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text(
        f"<b>{SHOP_NAME} | راهنما</b>\n\n"
        "🛍 <b>خرید:</b> محصول را انتخاب و خرید کنید.\n"
        "🛒 <b>سبد:</b> چند محصول را هم‌زمان مدیریت کنید.\n"
        "💰 <b>کیف پول:</b> اعتبار خود را شارژ کنید.\n"
        "🎁 <b>دعوت:</b> با دعوت دوستان پاداش بگیرید.\n"
        "🎟 <b>کوپن:</b> کد تخفیف را در Checkout اعمال کنید.\n"
        "🆘 <b>پشتیبانی:</b> برای مشکلات تیکت ثبت کنید.\n\n"
        "پس از تأیید پرداخت، کانفیگ از موجودی به‌صورت خودکار تحویل می‌شود.",
        reply_markup=back_home(),
    )


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

async def product_rows(where="", params=()):
    q = """
    SELECT p.*,
           COALESCE((SELECT COUNT(*) FROM configs c
                     WHERE c.product_id=p.id AND c.status='AVAILABLE'),0) AS stock
    FROM products p
    """
    if where:
        q += " WHERE " + where
    q += " ORDER BY p.featured DESC, p.id DESC"
    return await db.all(q, params)


@dp.callback_query(F.data == "shop")
async def cb_shop(c: CallbackQuery):
    await c.answer()
    cats = await db.all("SELECT * FROM categories WHERE active=1 ORDER BY sort_order,id")
    await c.message.edit_text(
        f"<b>🛍 فروشگاه {SHOP_NAME}</b>\n\nدسته‌بندی را انتخاب کنید:",
        reply_markup=shop_kb(cats),
    )


@dp.callback_query(F.data == "featured")
async def cb_featured(c: CallbackQuery):
    await c.answer()
    rows = await product_rows("p.active=1 AND p.featured=1")
    await c.message.edit_text(
        "<b>🔥 پیشنهادهای ویژه</b>\n\n" + (
            "محصولی ثبت نشده است." if not rows else "محصول مورد نظر را انتخاب کنید:"
        ),
        reply_markup=products_kb(rows) if rows else back_home(),
    )


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(c: CallbackQuery):
    await c.answer()
    cid = int(c.data.split(":")[1])
    cat = await db.one("SELECT * FROM categories WHERE id=%s", (cid,))
    rows = await product_rows("p.active=1 AND p.category_id=%s", (cid,))
    title = f'{cat["emoji"]} {esc(cat["name"])}' if cat else "محصولات"
    await c.message.edit_text(
        f"<b>{title}</b>\n\nمحصول مورد نظر را انتخاب کنید:",
        reply_markup=products_kb(rows) if rows else back_home(),
    )


@dp.callback_query(F.data.startswith("product:"))
async def cb_product(c: CallbackQuery):
    await c.answer()
    pid = int(c.data.split(":")[1])
    p = await db.one(
        """SELECT p.*,COALESCE((SELECT COUNT(*) FROM configs c
        WHERE c.product_id=p.id AND c.status='AVAILABLE'),0) stock
        FROM products p WHERE p.id=%s""",
        (pid,),
    )
    if not p:
        await c.message.edit_text("❌ محصول پیدا نشد.", reply_markup=back_home())
        return
    fav = await db.one(
        "SELECT 1 FROM favorites f JOIN users u ON u.id=f.user_id WHERE u.telegram_id=%s AND f.product_id=%s",
        (c.from_user.id, pid),
    )
    text = (
        f"<b>{esc(p['name'])}</b>\n\n"
        f"🔐 پروتکل: <b>{esc(p['protocol'])}</b>\n"
        f"📊 ترافیک: <b>{p['traffic_gb']}GB</b>\n"
        f"⏳ مدت: <b>{p['duration_days']} روز</b>\n"
        f"📱 دستگاه: <b>{p['devices']}</b>\n"
        f"📦 موجودی: <b>{p['stock']}</b>\n"
        f"💰 قیمت: <b>{money(p['price'])}</b>\n\n"
        f"ℹ️ {esc(p['description'])}"
    )
    await c.message.edit_text(text, reply_markup=product_detail_kb(pid, bool(fav)))


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy_now(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    product = await db.one(
        "SELECT id,active FROM products WHERE id=%s",
        (pid,),
    )
    if not user or not product or not product["active"]:
        await c.answer("❌ این محصول در دسترس نیست.", show_alert=True)
        return
    stock = await db.one(
        "SELECT COUNT(*) AS c FROM configs WHERE product_id=%s AND status='AVAILABLE'",
        (pid,),
    )
    if not stock or stock["c"] < 1:
        await c.answer("❌ موجودی این سرویس تمام شده است.", show_alert=True)
        return
    await db.run(
        """INSERT INTO cart_items(user_id,product_id,quantity) VALUES(%s,%s,1)
           ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=1""",
        (user["id"], pid),
    )
    await cb_checkout(c)


@dp.callback_query(F.data.startswith("addcart:"))
async def cb_addcart(c: CallbackQuery):
    await c.answer("به سبد اضافه شد 🛒")
    pid = int(c.data.split(":")[1])
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    await db.run(
        """INSERT INTO cart_items(user_id,product_id,quantity) VALUES(%s,%s,1)
           ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=quantity+1""",
        (user["id"], pid),
    )


@dp.callback_query(F.data == "cart")
async def cb_cart(c: CallbackQuery):
    await c.answer()
    user = await db.one("SELECT id,balance FROM users WHERE telegram_id=%s", (c.from_user.id,))
    items = await db.all(
        """SELECT ci.*,p.name,p.price,p.traffic_gb,p.duration_days,
                  (ci.quantity*p.price) line_total
           FROM cart_items ci JOIN products p ON p.id=ci.product_id
           WHERE ci.user_id=%s ORDER BY ci.id""",
        (user["id"],),
    )
    if not items:
        await c.message.edit_text("🛒 سبد خرید شما خالی است.", reply_markup=back_home())
        return
    total = sum(x["line_total"] for x in items)
    text = "<b>🛒 سبد خرید</b>\n\n"
    b = InlineKeyboardBuilder()
    for x in items:
        text += f"• {esc(x['name'])} × {x['quantity']} = <b>{money(x['line_total'])}</b>\n"
        b.row(
            InlineKeyboardButton(text=f"➖ {x['name']}", callback_data=f"cartminus:{x['product_id']}"),
            InlineKeyboardButton(text=f"➕", callback_data=f"cartplus:{x['product_id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"cartdel:{x['product_id']}"),
        )
    text += f"\n💰 جمع: <b>{money(total)}</b>\n💳 کیف پول: <b>{money(user['balance'])}</b>"
    b.row(InlineKeyboardButton(text="🎟 اعمال کد تخفیف", callback_data="coupon"))
    b.row(InlineKeyboardButton(text="💳 تسویه حساب", callback_data="checkout"))
    b.row(InlineKeyboardButton(text="🗑 خالی کردن سبد", callback_data="cartclear"))
    b.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home"))
    await c.message.edit_text(text, reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith(("cartplus:", "cartminus:", "cartdel:")))
async def cb_cart_change(c: CallbackQuery):
    action, pid_s = c.data.split(":")
    pid = int(pid_s)
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    item = await db.one("SELECT quantity FROM cart_items WHERE user_id=%s AND product_id=%s", (user["id"], pid))
    if item:
        if action == "cartplus":
            await db.run("UPDATE cart_items SET quantity=quantity+1 WHERE user_id=%s AND product_id=%s", (user["id"], pid))
        elif action == "cartminus":
            if item["quantity"] <= 1:
                await db.run("DELETE FROM cart_items WHERE user_id=%s AND product_id=%s", (user["id"], pid))
            else:
                await db.run("UPDATE cart_items SET quantity=quantity-1 WHERE user_id=%s AND product_id=%s", (user["id"], pid))
        else:
            await db.run("DELETE FROM cart_items WHERE user_id=%s AND product_id=%s", (user["id"], pid))
    await c.answer()
    await cb_cart(c)


@dp.callback_query(F.data == "cartclear")
async def cb_cartclear(c: CallbackQuery):
    await c.answer("سبد خالی شد")
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    await db.run("DELETE FROM cart_items WHERE user_id=%s", (user["id"],))
    await cb_cart(c)


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    row = await db.one("SELECT id FROM favorites WHERE user_id=%s AND product_id=%s", (user["id"], pid))
    if row:
        await db.run("DELETE FROM favorites WHERE id=%s", (row["id"],))
        await c.answer("از علاقه‌مندی‌ها حذف شد 💔")
    else:
        await db.run(
            "INSERT INTO favorites(user_id,product_id,created_at) VALUES(%s,%s,%s) ON CONFLICT(user_id,product_id) DO NOTHING",
            (user["id"], pid, now_iso()),
        )
        await c.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
    await cb_product(c)


@dp.callback_query(F.data == "favorites")
async def cb_favorites(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT p.*,COALESCE((SELECT COUNT(*) FROM configs c
        WHERE c.product_id=p.id AND c.status='AVAILABLE'),0) stock
        FROM favorites f JOIN products p ON p.id=f.product_id
        JOIN users u ON u.id=f.user_id
        WHERE u.telegram_id=%s AND p.active=1 ORDER BY f.id DESC""",
        (c.from_user.id,),
    )
    await c.message.edit_text(
        "<b>❤️ علاقه‌مندی‌ها</b>\n\nانتخاب کنید:",
        reply_markup=products_kb(rows) if rows else back_home(),
    )


# ---------------------------------------------------------------------------
# Checkout and payment
# ---------------------------------------------------------------------------

async def get_cart(user_id):
    return await db.all(
        """SELECT ci.*,p.name,p.price,p.active,
                  (ci.quantity*p.price) line_total
           FROM cart_items ci JOIN products p ON p.id=ci.product_id
           WHERE ci.user_id=%s ORDER BY ci.id""",
        (user_id,),
    )


async def validate_coupon(user_id, code, subtotal):
    coupon = await db.one(
        """SELECT * FROM coupons
           WHERE code=%s AND active=1
           AND (starts_at IS NULL OR starts_at<=%s)
           AND (ends_at IS NULL OR ends_at>=%s)""",
        (code.upper().strip(), now_iso(), now_iso()),
    )
    if not coupon:
        return None, 0, "کد تخفیف معتبر نیست."
    if subtotal < coupon["min_order"]:
        return coupon, 0, f"حداقل مبلغ سفارش برای این کد {money(coupon['min_order'])} است."
    if coupon["usage_limit"] is not None and coupon["used_count"] >= coupon["usage_limit"]:
        return coupon, 0, "ظرفیت استفاده از این کد تمام شده است."
    used = await db.one(
        "SELECT COUNT(*) c FROM coupon_uses WHERE coupon_id=%s AND user_id=%s",
        (coupon["id"], user_id),
    )
    if used["c"] >= coupon["per_user_limit"]:
        return coupon, 0, "شما قبلاً به سقف استفاده از این کد رسیده‌اید."
    if coupon["kind"] == "percent":
        discount = subtotal * coupon["value"] // 100
    else:
        discount = coupon["value"]
    if coupon["max_discount"] is not None:
        discount = min(discount, coupon["max_discount"])
    discount = min(discount, subtotal)
    return coupon, discount, ""


@dp.callback_query(F.data == "coupon")
async def cb_coupon(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(CouponState.waiting_code)
    await c.message.answer("🎟 کد تخفیف را ارسال کنید:")


@dp.message(CouponState.waiting_code)
async def receive_coupon(message: Message, state: FSMContext):
    await state.clear()
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (message.from_user.id,))
    items = await get_cart(user["id"])
    if not items:
        await message.answer("🛒 ابتدا محصولی به سبد اضافه کنید.")
        return
    subtotal = sum(i["line_total"] for i in items)
    coupon, discount, error = await validate_coupon(user["id"], message.text or "", subtotal)
    if error:
        await message.answer(f"❌ {error}")
        return
    await db.run(
        "INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (f"cart_coupon:{user['id']}", str(coupon["id"])),
    )
    await message.answer(
        f"✅ کد <b>{esc(coupon['code'])}</b> اعمال شد.\n"
        f"💸 تخفیف: <b>{money(discount)}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 تسویه حساب", callback_data="checkout")],
                [InlineKeyboardButton(text="🛒 سبد خرید", callback_data="cart")],
            ]
        ),
    )


async def create_order(user_id: int, payment_method: str, wallet_requested=True):
    async with await db.connect() as conn:
        await conn.execute("BEGIN")
        user = await (await conn.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,))).fetchone()
        items = await (await conn.execute(
            """SELECT ci.*,p.name,p.price,p.active
               FROM cart_items ci JOIN products p ON p.id=ci.product_id
               WHERE ci.user_id=%s""", (user_id,)
        )).fetchall()
        if not items:
            raise ValueError("سبد خرید خالی است.")
        if any(not i["active"] for i in items):
            raise ValueError("یکی از محصولات دیگر فعال نیست.")

        for i in items:
            stock = await (await conn.execute(
                "SELECT COUNT(*) AS c FROM configs WHERE product_id=%s AND status='AVAILABLE'",
                (i["product_id"],),
            )).fetchone()
            if stock["c"] < i["quantity"]:
                raise ValueError(f"موجودی «{i['name']}» کافی نیست. موجودی فعلی: {stock['c']}")

        subtotal = sum(i["quantity"] * i["price"] for i in items)
        coupon_id = None
        discount = 0
        setting = await (await conn.execute(
            "SELECT value FROM settings WHERE key=%s", (f"cart_coupon:{user_id}",)
        )).fetchone()
        if setting:
            coupon_id = int(setting["value"])
            coupon = await (await conn.execute("SELECT * FROM coupons WHERE id=%s", (coupon_id,))).fetchone()
            if coupon and coupon["active"]:
                current = now_iso()
                valid_window = (
                    (coupon["starts_at"] is None or coupon["starts_at"] <= current)
                    and (coupon["ends_at"] is None or coupon["ends_at"] >= current)
                )
                available = (
                    coupon["usage_limit"] is None
                    or coupon["used_count"] < coupon["usage_limit"]
                )
                used = await (await conn.execute(
                    "SELECT COUNT(*) AS c FROM coupon_uses WHERE coupon_id=%s AND user_id=%s",
                    (coupon_id, user_id),
                )).fetchone()
                if not valid_window or not available or used["c"] >= coupon["per_user_limit"]:
                    coupon_id = None
                    coupon = None
                if coupon:
                    if coupon["kind"] == "percent":
                        discount = subtotal * coupon["value"] // 100
                    else:
                        discount = coupon["value"]
                    if coupon["max_discount"] is not None:
                        discount = min(discount, coupon["max_discount"])
                    discount = min(discount, subtotal)

        net = subtotal - discount
        wallet_used = min(user["balance"], net) if wallet_requested else 0
        total = net - wallet_used

        code = make_code()
        cur = await conn.execute(
            """INSERT INTO orders
            (order_code,user_id,subtotal,discount,wallet_used,total,status,payment_method,coupon_id,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""",
            (
                code, user_id, subtotal, discount, wallet_used, total,
                "PENDING_PAYMENT" if total > 0 else "PAID",
                payment_method, coupon_id, now_iso(),
            ),
        )
        order_id = (await cur.fetchone())["id"]
        for i in items:
            await conn.execute(
                """INSERT INTO order_items(order_id,product_id,quantity,unit_price,product_name)
                   VALUES(%s,%s,%s,%s,%s)""",
                (order_id, i["product_id"], i["quantity"], i["price"], i["name"]),
            )
        if wallet_used:
            before = user["balance"]
            after = before - wallet_used
            await conn.execute("UPDATE users SET balance=%s,updated_at=%s WHERE id=%s", (after, now_iso(), user_id))
            await conn.execute(
                """INSERT INTO wallet_transactions
                (user_id,amount,balance_before,balance_after,kind,reference_id,note,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (user_id, -wallet_used, before, after, "ORDER", str(order_id), f"Order {code}", now_iso()),
            )
        if coupon_id:
            await conn.execute(
                "UPDATE coupons SET used_count=used_count+1 WHERE id=%s", (coupon_id,)
            )
            await conn.execute(
                """INSERT INTO coupon_uses(coupon_id,user_id,order_id,used_at)
                   VALUES(%s,%s,%s,%s)""",
                (coupon_id, user_id, order_id, now_iso()),
            )
            await conn.execute("DELETE FROM settings WHERE key=%s", (f"cart_coupon:{user_id}",))
        await conn.execute("DELETE FROM cart_items WHERE user_id=%s", (user_id,))
        await conn.commit()
        return order_id, code, subtotal, discount, wallet_used, total


async def complete_paid_order(order_id: int):
    async with await db.connect() as conn:
        await conn.execute("BEGIN")
        order = await (await conn.execute("SELECT * FROM orders WHERE id=%s", (order_id,))).fetchone()
        if not order:
            raise ValueError("Order not found.")
        if order["status"] == "COMPLETED":
            await conn.commit()
            return []
        items = await (await conn.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,))).fetchall()
        delivered = []
        for item in items:
            for _ in range(item["quantity"]):
                cfg = await (await conn.execute(
                    """SELECT * FROM configs
                       WHERE product_id=%s AND status='AVAILABLE'
                       ORDER BY id LIMIT 1""",
                    (item["product_id"],)
                )).fetchone()
                if not cfg:
                    raise ValueError(f"موجودی محصول «{item['product_name']}» کافی نیست.")
                await conn.execute(
                    """UPDATE configs SET status='SOLD',sold_to=%s,sold_order_id=%s,sold_at=%s
                       WHERE id=%s AND status='AVAILABLE'""",
                    (order["user_id"], order_id, now_iso(), cfg["id"]),
                )
                delivered.append((item["product_name"], cfg["config"]))
        await conn.execute(
            """UPDATE orders SET status='COMPLETED',paid_at=COALESCE(paid_at,%s),
               completed_at=%s WHERE id=%s""",
            (now_iso(), now_iso(), order_id),
        )
        await conn.commit()
        return delivered


async def send_delivery(order_id: int):
    order = await db.one("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not order:
        return
    user = await db.one("SELECT telegram_id FROM users WHERE id=%s", (order["user_id"],))
    if not user:
        log.error("Delivery failed: user not found for order %s", order_id)
        return
    try:
        delivered = await complete_paid_order(order_id)
    except Exception as e:
        log.exception("Delivery failed for order %s", order_id)
        await bot.send_message(
            user["telegram_id"],
            f"⚠️ پرداخت سفارش <b>{esc(order['order_code'])}</b> ثبت شد، اما موجودی کافی برای تحویل خودکار نیست.\n"
            "لطفاً با پشتیبانی تماس بگیرید.",
        )
        return

    lines = [
        "🎉 <b>خرید شما با موفقیت تکمیل شد!</b>",
        f"🧾 سفارش: <b>{esc(order['order_code'])}</b>",
        "",
    ]
    for name, cfg in delivered:
        lines += [f"📦 <b>{esc(name)}</b>", f"<code>{esc(cfg)}</code>", ""]
    await bot.send_message(user["telegram_id"], "\n".join(lines))


@dp.callback_query(F.data == "checkout")
async def cb_checkout(c: CallbackQuery):
    await c.answer()
    user = await db.one("SELECT * FROM users WHERE telegram_id=%s", (c.from_user.id,))
    try:
        order_id, code, subtotal, discount, wallet_used, total = await create_order(user["id"], "MANUAL", True)
    except Exception as e:
        await c.message.edit_text(f"❌ {esc(e)}", reply_markup=back_home())
        return

    if total == 0:
        await send_delivery(order_id)
        return

    payment = await db.run(
        """INSERT INTO payments(order_id,user_id,amount,method,status,created_at)
           VALUES(%s,%s,%s,%s,%s,%s)""",
        (order_id, user["id"], total, "MANUAL_CARD", "PENDING", now_iso()),
    )
    card = CARD_NUMBER or "شماره کارت در تنظیمات ثبت نشده است"
    owner = CARD_OWNER or "نام صاحب کارت در تنظیمات ثبت نشده است"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📎 ارسال رسید پرداخت", callback_data=f"receipt:{order_id}")],
            [InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"cancelorder:{order_id}")],
        ]
    )
    await c.message.edit_text(
        f"<b>💳 پرداخت سفارش</b>\n\n"
        f"🧾 سفارش: <b>{esc(code)}</b>\n"
        f"🛍 جمع: {money(subtotal)}\n"
        f"🎟 تخفیف: {money(discount)}\n"
        f"💰 استفاده از کیف پول: {money(wallet_used)}\n"
        f"💳 مبلغ قابل پرداخت: <b>{money(total)}</b>\n\n"
        f"💳 شماره کارت:\n<code>{esc(card)}</code>\n"
        f"👤 به نام: <b>{esc(owner)}</b>\n\n"
        "پس از پرداخت، روی «ارسال رسید» بزنید و تصویر رسید را ارسال کنید.",
        reply_markup=kb,
    )


class ReceiptState(StatesGroup):
    waiting_receipt = State()


@dp.callback_query(F.data.startswith("receipt:"))
async def cb_receipt(c: CallbackQuery, state: FSMContext):
    await c.answer()
    oid = int(c.data.split(":")[1])
    await state.set_state(ReceiptState.waiting_receipt)
    await state.update_data(order_id=oid)
    await c.message.answer("📎 لطفاً تصویر رسید پرداخت را ارسال کنید.")


@dp.message(ReceiptState.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    oid = int(data["order_id"])
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (message.from_user.id,))
    order = await db.one("SELECT * FROM orders WHERE id=%s AND user_id=%s", (oid, user["id"]))
    if not order:
        await message.answer("❌ سفارش پیدا نشد.")
        return
    if order["status"] in ("CANCELLED", "COMPLETED"):
        await message.answer("ℹ️ این سفارش دیگر قابل پرداخت نیست.")
        return
    photo = message.photo[-1]
    existing = await db.one(
        "SELECT id, receipt_file_id FROM payments WHERE order_id=%s AND status='PENDING' ORDER BY id DESC LIMIT 1",
        (oid,),
    )
    if existing and existing.get("receipt_file_id"):
        await message.answer("ℹ️ این سفارش قبلاً رسیدی در انتظار بررسی دارد.")
        return
    if existing:
        # The pending payment row is created when the order is opened.
        # Attach the uploaded receipt to that row rather than creating a duplicate.
        pid = existing["id"]
        await db.run(
            "UPDATE payments SET receipt_file_id=%s, method=%s WHERE id=%s",
            (photo.file_id, "MANUAL_CARD", pid),
        )
    else:
        pid = await db.run(
            """INSERT INTO payments(order_id,user_id,amount,method,receipt_file_id,status,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (oid, user["id"], order["total"], "MANUAL_CARD", photo.file_id, "PENDING", now_iso()),
        )
    await message.answer(
        f"✅ رسید دریافت شد.\n🧾 سفارش: <b>{esc(order['order_code'])}</b>\n"
        "پس از بررسی ادمین، نتیجه اعلام می‌شود."
    )
    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="✅ تأیید", callback_data=f"payapprove:{pid}"),
                    InlineKeyboardButton(text="❌ رد", callback_data=f"payreject:{pid}"),
                ]]
            )
            await bot.send_photo(
                admin_id,
                photo.file_id,
                caption=(
                    f"💳 <b>پرداخت جدید</b>\n\n"
                    f"🧾 {esc(order['order_code'])}\n"
                    f"👤 User: <code>{message.from_user.id}</code>\n"
                    f"💰 {money(order['total'])}"
                ),
                reply_markup=kb,
            )
        except Exception:
            log.exception("Could not notify admin %s", admin_id)


@dp.message(Command("receipt"))
async def receipt_command(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("فرمت: /receipt ORDER_CODE\nمثال: /receipt VSB-ABC123")
        return
    code = parts[1].strip().upper()
    user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (message.from_user.id,))
    order = await db.one(
        "SELECT * FROM orders WHERE order_code=%s AND user_id=%s",
        (code, user["id"]),
    )
    if not order:
        await message.answer("❌ سفارش پیدا نشد.")
        return
    if order["status"] in ("CANCELLED", "COMPLETED"):
        await message.answer("ℹ️ این سفارش دیگر نیاز به رسید ندارد.")
        return
    await state.set_state(ReceiptState.waiting_receipt)
    await state.update_data(order_id=order["id"])
    await message.answer("📎 حالا تصویر رسید را ارسال کنید.")

@dp.message(ReceiptState.waiting_receipt)
async def receipt_wrong_type(message: Message):
    await message.answer("📎 لطفاً تصویر رسید را به‌صورت عکس ارسال کنید.")


@dp.callback_query(F.data.startswith("cancelorder:"))
async def cb_cancel_order(c: CallbackQuery):
    await c.answer()
    oid = int(c.data.split(":")[1])
    order = await db.one("SELECT * FROM orders WHERE id=%s", (oid,))
    current_user = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    if not order or not current_user or order["user_id"] != current_user["id"]:
        await c.message.edit_text("❌ سفارش معتبر نیست.", reply_markup=back_home())
        return
    if order["status"] in ("COMPLETED", "CANCELLED"):
        await c.message.edit_text("ℹ️ این سفارش قابل لغو نیست.", reply_markup=back_home())
        return
    await db.run("UPDATE orders SET status='CANCELLED' WHERE id=%s", (oid,))
    if order["wallet_used"]:
        await wallet_add(order["user_id"], order["wallet_used"], "REFUND", str(oid), "Order cancelled")
    await c.message.edit_text("✅ سفارش لغو شد و سهم کیف پول به حساب بازگردانده شد.", reply_markup=back_home())


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery):
    await c.answer()
    u = await db.one("SELECT * FROM users WHERE telegram_id=%s", (c.from_user.id,))
    tx = await db.all(
        "SELECT * FROM wallet_transactions WHERE user_id=%s ORDER BY id DESC LIMIT 5",
        (u["id"],),
    )
    text = f"<b>💰 کیف پول</b>\n\nموجودی: <b>{money(u['balance'])}</b>\n\n"
    if tx:
        text += "📜 آخرین تراکنش‌ها:\n"
        for t in tx:
            sign = "+" if t["amount"] > 0 else ""
            text += f"• {sign}{money(t['amount'])} — {esc(t['kind'])}\n"
    else:
        text += "تراکنشی وجود ندارد."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data="walletadd")],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")],
        ]
    )
    await c.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "walletadd")
async def cb_walletadd(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(WalletState.waiting_amount)
    await c.message.answer("💰 مبلغ شارژ را به تومان ارسال کنید (مثلاً 500000):")


@dp.message(WalletState.waiting_amount)
async def receive_wallet_amount(message: Message, state: FSMContext):
    await state.clear()
    try:
        amount = int((message.text or "").replace(",", "").strip())
        if amount < 1000:
            raise ValueError
    except ValueError:
        await message.answer("❌ مبلغ نامعتبر است.")
        return
    u = await db.one("SELECT * FROM users WHERE telegram_id=%s", (message.from_user.id,))
    order_code = make_code("WAL")
    oid = await db.run(
        """INSERT INTO orders(order_code,user_id,subtotal,discount,wallet_used,total,status,payment_method,created_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (order_code, u["id"], amount, 0, 0, amount, "PENDING_PAYMENT", "WALLET_TOPUP", now_iso()),
    )
    payment_id = await db.run(
        """INSERT INTO payments(order_id,user_id,amount,method,status,created_at)
           VALUES(%s,%s,%s,%s,%s,%s)""",
        (oid, u["id"], amount, "WALLET_TOPUP", "PENDING", now_iso()),
    )
    card = CARD_NUMBER or "شماره کارت در تنظیمات ثبت نشده است"
    owner = CARD_OWNER or "نام صاحب کارت در تنظیمات ثبت نشده است"
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📎 ارسال رسید شارژ", callback_data=f"receipt:{oid}")],
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")],
        ]
    )
    await message.answer(
        f"<b>➕ شارژ کیف پول</b>\n\n"
        f"🧾 شماره پیگیری: <b>{esc(order_code)}</b>\n"
        f"مبلغ: <b>{money(amount)}</b>\n"
        f"💳 کارت: <code>{esc(card)}</code>\n"
        f"👤 به نام: {esc(owner)}\n\n"
        "پس از پرداخت، روی دکمهٔ «ارسال رسید شارژ» بزنید و عکس رسید را بفرستید.",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    await c.answer()
    u = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    rows = await db.all(
        "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 15",
        (u["id"],),
    )
    if not rows:
        await c.message.edit_text("📦 هنوز سفارشی ندارید.", reply_markup=back_home())
        return
    text = "<b>📦 سفارش‌های من</b>\n\n"
    for o in rows:
        text += (
            f"🧾 <b>{esc(o['order_code'])}</b>\n"
            f"💰 {money(o['total'])}\n"
            f"📌 {esc(o['status'])}\n"
            f"🕐 {esc(o['created_at'][:19].replace('T',' '))}\n\n"
        )
    await c.message.edit_text(text, reply_markup=back_home())


# ---------------------------------------------------------------------------
# Referral
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "referral")
async def cb_referral(c: CallbackQuery):
    await c.answer()
    u = await db.one("SELECT * FROM users WHERE telegram_id=%s", (c.from_user.id,))
    count = await db.one("SELECT COUNT(*) c FROM referrals WHERE inviter_id=%s", (u["id"],))
    rewarded = await db.one(
        "SELECT COALESCE(SUM(reward_amount),0) s FROM referrals WHERE inviter_id=%s AND rewarded=1",
        (u["id"],),
    )
    me = await bot.get_me()
    link = f"https://t.me/{me.username}%sstart=ref_{c.from_user.id}"
    await c.message.edit_text(
        f"<b>🎁 دعوت دوستان</b>\n\n"
        f"🔗 لینک شما:\n<code>{esc(link)}</code>\n\n"
        f"👥 دعوت‌ها: <b>{count['c']}</b>\n"
        f"💰 پاداش دریافت‌شده: <b>{money(rewarded['s'])}</b>\n"
        f"🎁 پاداش فعلی هر دعوت: <b>{money(REFERRAL_REWARD)}</b>\n\n"
        "پاداش پس از اولین خرید موفق و تکمیل‌شده دوست شما فعال می‌شود.",
        reply_markup=back_home(),
    )


async def reward_referral_if_needed(user_id: int):
    user = await db.one("SELECT * FROM users WHERE id=%s", (user_id,))
    if not user or not user["referred_by"]:
        return
    ref = await db.one(
        "SELECT * FROM referrals WHERE invited_id=%s AND rewarded=0", (user_id,)
    )
    if not ref:
        return
    reward = REFERRAL_REWARD
    await wallet_add(
        ref["inviter_id"], reward, "REFERRAL", str(user_id), "First completed purchase referral reward"
    )
    await db.run(
        "UPDATE referrals SET rewarded=1,reward_amount=%s WHERE id=%s",
        (reward, ref["id"]),
    )
    inviter = await db.one("SELECT telegram_id FROM users WHERE id=%s", (ref["inviter_id"],))
    if inviter:
        try:
            await bot.send_message(
                inviter["telegram_id"],
                f"🎉 تبریک!\n{money(reward)} پاداش دعوت دوست به کیف پول شما اضافه شد.",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    await c.answer()
    u = await db.one("SELECT * FROM users WHERE telegram_id=%s", (c.from_user.id,))
    orders = await db.one("SELECT COUNT(*) c FROM orders WHERE user_id=%s", (u["id"],))
    completed = await db.one("SELECT COUNT(*) c FROM orders WHERE user_id=%s AND status='COMPLETED'", (u["id"],))
    await c.message.edit_text(
        f"<b>👤 حساب کاربری</b>\n\n"
        f"🆔 Telegram ID: <code>{c.from_user.id}</code>\n"
        f"👤 نام: {esc(u['first_name'] or '')}\n"
        f"🔹 Username: @{esc(u['username']) if u['username'] else '-'}\n"
        f"🏆 سطح: <b>{esc(u['level'])}</b>\n"
        f"💰 موجودی: <b>{money(u['balance'])}</b>\n"
        f"🧾 سفارش‌ها: <b>{orders['c']}</b>\n"
        f"✅ تکمیل‌شده: <b>{completed['c']}</b>",
        reply_markup=back_home(),
    )


# ---------------------------------------------------------------------------
# Support / tickets
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    await c.answer()
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🎫 تیکت جدید", callback_data="ticketnew"))
    b.row(InlineKeyboardButton(text="📋 تیکت‌های من", callback_data="mytickets"))
    b.row(InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home"))
    await c.message.edit_text(
        "<b>🆘 پشتیبانی</b>\n\nیکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data == "ticketnew")
async def cb_ticketnew(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(TicketState.waiting_subject)
    await c.message.answer("🎫 موضوع تیکت را ارسال کنید:")


@dp.message(TicketState.waiting_subject)
async def ticket_subject(message: Message, state: FSMContext):
    await state.update_data(subject=(message.text or "بدون موضوع")[:200])
    await state.set_state(TicketState.waiting_message)
    await message.answer("💬 متن مشکل یا درخواست خود را ارسال کنید:")


@dp.message(TicketState.waiting_message)
async def ticket_message(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    u = await db.one("SELECT id FROM users WHERE telegram_id=%s", (message.from_user.id,))
    tid = await db.run(
        """INSERT INTO tickets(user_id,subject,status,priority,created_at,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s)""",
        (u["id"], data["subject"], "OPEN", "NORMAL", now_iso(), now_iso()),
    )
    await db.run(
        "INSERT INTO ticket_messages(ticket_id,sender_id,message,created_at) VALUES(%s,%s,%s,%s)",
        (tid, u["id"], message.text or "", now_iso()),
    )
    await message.answer(f"✅ تیکت <b>#{tid}</b> ثبت شد.\nپشتیبانی به‌زودی پاسخ می‌دهد.")
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid,
                f"🎫 <b>تیکت جدید #{tid}</b>\n"
                f"👤 User: <code>{message.from_user.id}</code>\n"
                f"📝 {esc(data['subject'])}\n\n"
                f"{esc(message.text or '')}",
            )
        except Exception:
            pass


@dp.callback_query(F.data == "mytickets")
async def cb_mytickets(c: CallbackQuery):
    await c.answer()
    u = await db.one("SELECT id FROM users WHERE telegram_id=%s", (c.from_user.id,))
    rows = await db.all(
        "SELECT * FROM tickets WHERE user_id=%s ORDER BY id DESC LIMIT 10", (u["id"],)
    )
    if not rows:
        await c.message.edit_text("🎫 تیکتی ندارید.", reply_markup=back_home())
        return
    text = "<b>🎫 تیکت‌های من</b>\n\n"
    for t in rows:
        text += f"#{t['id']} — {esc(t['subject'])} — <b>{t['status']}</b>\n"
    await c.message.edit_text(text, reply_markup=back_home())


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "admin")
async def cb_admin(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        await c.message.edit_text("⛔ دسترسی ندارید.", reply_markup=back_home())
        return
    await c.message.edit_text("<b>👑 پنل مدیریت</b>\n\nمدیریت کامل فروشگاه:", reply_markup=admin_kb())


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ دسترسی ندارید.")
        return
    await message.answer("<b>👑 پنل مدیریت</b>", reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:dashboard")
async def adm_dashboard(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    users = await db.one("SELECT COUNT(*) c FROM users")
    orders = await db.one("SELECT COUNT(*) c FROM orders")
    completed = await db.one("SELECT COUNT(*) c FROM orders WHERE status='COMPLETED'")
    revenue = await db.one("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status='COMPLETED'")
    configs = await db.one("SELECT COUNT(*) c FROM configs WHERE status='AVAILABLE'")
    tickets = await db.one("SELECT COUNT(*) c FROM tickets WHERE status='OPEN'")
    today = datetime.now(timezone.utc).date().isoformat()
    today_orders = await db.one(
        "SELECT COUNT(*) c FROM orders WHERE created_at LIKE %s", (today + "%",)
    )
    today_rev = await db.one(
        "SELECT COALESCE(SUM(total),0) s FROM orders WHERE status='COMPLETED' AND completed_at LIKE %s",
        (today + "%",),
    )
    await c.message.edit_text(
        f"<b>📊 DASHBOARD</b>\n\n"
        f"👥 کاربران: <b>{users['c']}</b>\n"
        f"🧾 سفارش‌ها: <b>{orders['c']}</b>\n"
        f"✅ تکمیل‌شده: <b>{completed['c']}</b>\n"
        f"💰 درآمد: <b>{money(revenue['s'])}</b>\n"
        f"📦 موجودی کانفیگ: <b>{configs['c']}</b>\n"
        f"🎫 تیکت باز: <b>{tickets['c']}</b>\n\n"
        f"📅 امروز:\n"
        f"🧾 سفارش: <b>{today_orders['c']}</b>\n"
        f"💰 درآمد: <b>{money(today_rev['s'])}</b>",
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "adm:orders")
async def adm_orders(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id
           ORDER BY o.id DESC LIMIT 20"""
    )
    text = "<b>🧾 آخرین سفارش‌ها</b>\n\n"
    for o in rows:
        text += f"#{esc(o['order_code'])} | {money(o['total'])} | {esc(o['status'])} | {o['telegram_id']}\n"
    await c.message.edit_text(text[:3900], reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:payments")
async def adm_payments(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT p.*,o.order_code,u.telegram_id
           FROM payments p JOIN orders o ON o.id=p.order_id
           JOIN users u ON u.id=p.user_id
           WHERE p.status='PENDING' ORDER BY p.id DESC LIMIT 15"""
    )
    if not rows:
        await c.message.edit_text("💳 پرداخت معلقی وجود ندارد.", reply_markup=admin_kb())
        return
    b = InlineKeyboardBuilder()
    text = "<b>💳 پرداخت‌های در انتظار</b>\n\n"
    for p in rows:
        text += f"🧾 {esc(p['order_code'])} — {money(p['amount'])} — {p['telegram_id']}\n"
        b.row(
            InlineKeyboardButton(text=f"✅ تأیید {p['id']}", callback_data=f"payapprove:{p['id']}"),
            InlineKeyboardButton(text=f"❌ رد {p['id']}", callback_data=f"payreject:{p['id']}"),
        )
    b.row(InlineKeyboardButton(text="👑 پنل", callback_data="admin"))
    await c.message.edit_text(text, reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("payapprove:"))
async def adm_payapprove(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[1])
    p = await db.one("SELECT * FROM payments WHERE id=%s", (pid,))
    if not p or p["status"] != "PENDING":
        await c.message.answer("ℹ️ این پرداخت قبلاً بررسی شده است.")
        return
    if not p.get("receipt_file_id"):
        await c.message.answer("⚠️ برای این پرداخت هنوز رسیدی ثبت نشده است.")
        return
    order = await db.one("SELECT * FROM orders WHERE id=%s", (p["order_id"],))
    if not order:
        await c.message.answer("❌ سفارش مرتبط با این پرداخت پیدا نشد.")
        return
    if order["payment_method"] == "WALLET_TOPUP":
        # Mark the payment first; the pending-state check above makes this idempotent.
        await db.run(
            "UPDATE payments SET status='APPROVED',reviewed_at=%s,reviewed_by=%s WHERE id=%s AND status='PENDING'",
            (now_iso(), c.from_user.id, pid),
        )
        await wallet_add(order["user_id"], p["amount"], "TOPUP", str(order["id"]), "Manual payment approved")
        await db.run(
            "UPDATE orders SET status='COMPLETED',paid_at=%s,completed_at=%s WHERE id=%s",
            (now_iso(), now_iso(), order["id"]),
        )
        user = await db.one("SELECT telegram_id,balance FROM users WHERE id=%s", (order["user_id"],))
        await bot.send_message(user["telegram_id"], f"✅ شارژ کیف پول شما تأیید شد.\n💰 موجودی: <b>{money(user['balance'])}</b>")
    else:
        await db.run(
            "UPDATE orders SET status='PAID',paid_at=%s WHERE id=%s", (now_iso(), order["id"])
        )
        await db.run(
            "UPDATE payments SET status='APPROVED',reviewed_at=%s,reviewed_by=%s WHERE id=%s",
            (now_iso(), c.from_user.id, pid),
        )
        await send_delivery(order["id"])
        await reward_referral_if_needed(order["user_id"])
    await audit(c.from_user.id, "PAYMENT_APPROVED", pid, order["order_code"])
    try:
        await c.message.edit_caption(caption="✅ پرداخت تأیید شد.", reply_markup=admin_kb())
    except Exception:
        await c.message.edit_text("✅ پرداخت تأیید شد.", reply_markup=admin_kb())


@dp.callback_query(F.data.startswith("payreject:"))
async def adm_payreject(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    pid = int(c.data.split(":")[1])
    p = await db.one("SELECT * FROM payments WHERE id=%s", (pid,))
    if not p or p["status"] != "PENDING":
        return
    await db.run(
        "UPDATE payments SET status='REJECTED',reviewed_at=%s,reviewed_by=%s WHERE id=%s",
        (now_iso(), c.from_user.id, pid),
    )
    order = await db.one("SELECT * FROM orders WHERE id=%s", (p["order_id"],))
    if order:
        await db.run("UPDATE orders SET status='PAYMENT_REJECTED' WHERE id=%s", (order["id"],))
        user = await db.one("SELECT telegram_id FROM users WHERE id=%s", (order["user_id"],))
        if user:
            await bot.send_message(user["telegram_id"], f"❌ پرداخت سفارش <b>{esc(order['order_code'])}</b> رد شد. لطفاً رسید صحیح ارسال کنید.")
    await audit(c.from_user.id, "PAYMENT_REJECTED", pid, "")
    try:
        await c.message.edit_caption(caption="❌ پرداخت رد شد.", reply_markup=admin_kb())
    except Exception:
        await c.message.edit_text("❌ پرداخت رد شد.", reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:inventory")
async def adm_inventory(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT p.id,p.name,
        SUM(CASE WHEN c.status='AVAILABLE' THEN 1 ELSE 0 END) available,
        SUM(CASE WHEN c.status='SOLD' THEN 1 ELSE 0 END) sold
        FROM products p LEFT JOIN configs c ON c.product_id=p.id
        GROUP BY p.id ORDER BY p.id"""
    )
    text = "<b>📦 موجودی</b>\n\n"
    for r in rows:
        text += f"#{r['id']} {esc(r['name'])}: 🟢 {r['available']} | 🔴 {r['sold']}\n"
    text += "\nبرای افزودن کانفیگ: /addconfig PRODUCT_ID سپس متن کانفیگ"
    await c.message.edit_text(text, reply_markup=admin_kb())


@dp.message(Command("addconfig"))
async def addconfig_cmd(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("فرمت: /addconfig PRODUCT_ID")
        return
    await state.set_state(ConfigState.waiting_config)
    await state.update_data(product_id=int(parts[1]))
    await message.answer("🔐 کانفیگ را ارسال کنید:")


@dp.message(ConfigState.waiting_config)
async def receive_config(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    pid = int(data["product_id"])
    p = await db.one("SELECT name FROM products WHERE id=%s", (pid,))
    if not p:
        await message.answer("❌ محصول پیدا نشد.")
        return
    await db.run(
        "INSERT INTO configs(product_id,config,status,created_at) VALUES(%s,%s,%s,%s)",
        (pid, message.text or "", "AVAILABLE", now_iso()),
    )
    await audit(message.from_user.id, "CONFIG_ADDED", pid, p["name"])
    await message.answer(f"✅ کانفیگ برای «{esc(p['name'])}» اضافه شد.")


@dp.callback_query(F.data == "adm:products")
async def adm_products(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT p.*,COALESCE((SELECT COUNT(*) FROM configs c
        WHERE c.product_id=p.id AND c.status='AVAILABLE'),0) stock
        FROM products p ORDER BY p.id DESC"""
    )
    text = "<b>🛍 محصولات</b>\n\n"
    for p in rows:
        text += f"#{p['id']} {esc(p['name'])} — {money(p['price'])} — stock:{p['stock']}\n"
    text += "\nبرای افزودن محصول: /addproduct"
    await c.message.edit_text(text, reply_markup=admin_kb())


@dp.message(Command("addproduct"))
async def addproduct_cmd(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddProductState.waiting_name)
    await message.answer("🛍 نام محصول:")


@dp.message(AddProductState.waiting_name)
async def ap_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text or "Product")
    await state.set_state(AddProductState.waiting_protocol)
    await message.answer("🔐 پروتکل (VLESS/VMess/Trojan/SS):")


@dp.message(AddProductState.waiting_protocol)
async def ap_protocol(message: Message, state: FSMContext):
    await state.update_data(protocol=message.text or "VLESS")
    await state.set_state(AddProductState.waiting_price)
    await message.answer("💰 قیمت:")


@dp.message(AddProductState.waiting_price)
async def ap_price(message: Message, state: FSMContext):
    try:
        price = int((message.text or "").replace(",", ""))
    except ValueError:
        await message.answer("❌ قیمت عددی نیست.")
        return
    await state.update_data(price=price)
    await state.set_state(AddProductState.waiting_traffic)
    await message.answer("📊 حجم به GB:")


@dp.message(AddProductState.waiting_traffic)
async def ap_traffic(message: Message, state: FSMContext):
    try:
        traffic = int(message.text or "0")
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    await state.update_data(traffic=traffic)
    await state.set_state(AddProductState.waiting_duration)
    await message.answer("⏳ مدت به روز:")


@dp.message(AddProductState.waiting_duration)
async def ap_duration(message: Message, state: FSMContext):
    try:
        duration = int(message.text or "30")
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    await state.update_data(duration=duration)
    await state.set_state(AddProductState.waiting_devices)
    await message.answer("📱 تعداد دستگاه:")


@dp.message(AddProductState.waiting_devices)
async def ap_devices(message: Message, state: FSMContext):
    try:
        devices = int(message.text or "1")
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    data = await state.get_data()
    await state.clear()
    await db.run(
        """INSERT INTO products
        (name,protocol,description,traffic_gb,duration_days,devices,price,active,featured,created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (data["name"], data["protocol"], "", data["traffic"], data["duration"], devices, data["price"], 1, 0, now_iso()),
    )
    await audit(message.from_user.id, "PRODUCT_ADDED", "", data["name"])
    await message.answer(f"✅ محصول «{esc(data['name'])}» ساخته شد.")


@dp.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery):
    await c.answer()
    total = await db.one("SELECT COUNT(*) c FROM users")
    active = await db.one("SELECT COUNT(*) c FROM users WHERE is_banned=0")
    banned = await db.one("SELECT COUNT(*) c FROM users WHERE is_banned=1")
    await c.message.edit_text(
        f"<b>👥 کاربران</b>\n\n"
        f"کل: <b>{total['c']}</b>\n"
        f"فعال: <b>{active['c']}</b>\n"
        f"مسدود: <b>{banned['c']}</b>\n\n"
        "مدیریت کاربر با دستورات /ban USER_ID و /unban USER_ID.",
        reply_markup=admin_kb(),
    )


@dp.message(Command("ban"))
async def ban_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("فرمت: /ban TELEGRAM_ID")
        return
    tid = int(parts[1])
    await db.run("UPDATE users SET is_banned=1 WHERE telegram_id=%s", (tid,))
    await audit(message.from_user.id, "BAN_USER", tid, "")
    await message.answer("⛔ کاربر مسدود شد.")


@dp.message(Command("unban"))
async def unban_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("فرمت: /unban TELEGRAM_ID")
        return
    tid = int(parts[1])
    await db.run("UPDATE users SET is_banned=0 WHERE telegram_id=%s", (tid,))
    await audit(message.from_user.id, "UNBAN_USER", tid, "")
    await message.answer("✅ کاربر رفع مسدودی شد.")


@dp.callback_query(F.data == "adm:tickets")
async def adm_tickets(c: CallbackQuery):
    await c.answer()
    rows = await db.all(
        """SELECT t.*,u.telegram_id FROM tickets t JOIN users u ON u.id=t.user_id
           WHERE t.status='OPEN' ORDER BY t.id DESC LIMIT 20"""
    )
    text = "<b>🎫 تیکت‌های باز</b>\n\n"
    for t in rows:
        text += f"#{t['id']} | {esc(t['subject'])} | {t['telegram_id']} | {t['priority']}\n"
    await c.message.edit_text(text[:3900], reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(c: CallbackQuery, state: FSMContext):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_message)
    await c.message.answer("📢 پیام همگانی را ارسال کنید:")


@dp.message(BroadcastState.waiting_message)
async def receive_broadcast(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        return
    rows = await db.all("SELECT telegram_id FROM users WHERE is_banned=0")
    sent = failed = 0
    for r in rows:
        try:
            await bot.copy_message(
                chat_id=r["telegram_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await message.answer(f"📢 ارسال تمام شد.\n✅ موفق: {sent}\n❌ ناموفق: {failed}")
    await audit(message.from_user.id, "BROADCAST", "", f"sent={sent},failed={failed}")


@dp.callback_query(F.data == "adm:coupons")
async def adm_coupons(c: CallbackQuery):
    await c.answer()
    rows = await db.all("SELECT * FROM coupons ORDER BY id DESC LIMIT 30")
    text = "<b>🎟 کوپن‌ها</b>\n\n"
    for x in rows:
        text += f"<code>{esc(x['code'])}</code> | {x['kind']}:{x['value']} | used:{x['used_count']} | {'ON' if x['active'] else 'OFF'}\n"
    text += "\nایجاد: /addcoupon CODE PERCENT VALUE"
    await c.message.edit_text(text[:3900], reply_markup=admin_kb())


@dp.message(Command("addcoupon"))
async def addcoupon_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("فرمت: /addcoupon CODE percent VALUE\nمثال: /addcoupon WELCOME10 percent 10")
        return
    code, kind, value = parts[1].upper(), parts[2].lower(), int(parts[3])
    if kind not in ("percent", "fixed"):
        await message.answer("kind باید percent یا fixed باشد.")
        return
    try:
        await db.run(
            """INSERT INTO coupons(code,kind,value,active) VALUES(%s,%s,%s,1)""",
            (code, kind, value),
        )
    except pg_errors.UniqueViolation:
        await message.answer("❌ این کد تخفیف قبلاً وجود دارد.")
        return
    await audit(message.from_user.id, "COUPON_ADDED", code, f"{kind}:{value}")
    await message.answer(f"✅ کوپن <code>{esc(code)}</code> ساخته شد.")


@dp.callback_query(F.data == "adm:backup")
async def adm_backup(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return
    try:
        tables = ["users", "categories", "products", "configs", "cart_items", "coupons",
                  "coupon_uses", "orders", "order_items", "payments", "wallet_transactions",
                  "referrals", "tickets", "ticket_messages", "favorites", "admin_logs", "settings"]
        snapshot = {"created_at": now_iso(), "tables": {}}
        for table in tables:
            rows = await db.all(f"SELECT * FROM {table}")
            snapshot["tables"][table] = [dict(r) for r in rows]
        backup_dir = Path("backups")
        backup_dir.mkdir(exist_ok=True)
        target = backup_dir / f"v2ray_shop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        target.write_text(json.dumps(snapshot, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        await audit(c.from_user.id, "BACKUP_CREATED", str(target), "postgresql-json-snapshot")
        await c.message.answer(f"💾 بکاپ داده‌ها ساخته شد:\n<code>{esc(str(target))}</code>")
    except Exception as e:
        await c.message.answer(f"❌ خطا: {esc(e)}")

# ---------------------------------------------------------------------------
# REST / Render
# ---------------------------------------------------------------------------

async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "service": SHOP_NAME,
            "database": "postgresql",
            "time": now_iso(),
        }
    )


async def ready(request):
    try:
        row = await db.one("SELECT 1 AS ok")
        if not row or row["ok"] != 1:
            raise RuntimeError("database unavailable")
        return web.json_response({"status": "ready"})
    except Exception as e:
        return web.json_response({"status": "not_ready", "error": str(e)}, status=503)


def api_authorized(request):
    if not API_KEY:
        return True
    return request.headers.get("X-API-Key") == API_KEY


async def api_products(request):
    if not api_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    rows = await product_rows("p.active=1")
    return web.json_response([dict(r) for r in rows])


async def api_stats(request):
    if not API_KEY or not api_authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    users = await db.one("SELECT COUNT(*) c FROM users")
    orders = await db.one("SELECT COUNT(*) c FROM orders")
    revenue = await db.one("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status='COMPLETED'")
    return web.json_response(
        {"users": users["c"], "orders": orders["c"], "revenue": revenue["s"]}
    )


async def webhook(request):
    secret = request.match_info["secret"]
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        return web.Response(status=403)
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    try:
        from aiogram.types import Update
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Webhook update failed")
        return web.Response(status=500)
    return web.Response(text="OK")


async def on_startup():
    await db.init()
    if WEBHOOK_BASE_URL:
        url = f"{WEBHOOK_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}"
        await bot.set_webhook(url=url, drop_pending_updates=False)
        log.info("Webhook set: %s", url)
    else:
        log.info("WEBHOOK_BASE_URL not set; polling mode is used.")


async def on_shutdown():
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    await bot.session.close()


async def create_web_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/api/v1/products", api_products)
    app.router.add_get("/api/v1/stats", api_stats)
    app.router.add_post("/telegram/webhook/{secret}", webhook)
    return app


async def run_web_server():
    app = await create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on 0.0.0.0:%s", PORT)
    return runner


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

async def maintenance_loop():
    while True:
        try:
            # Release expired reservations if a future reservation feature is used.
            await db.run(
                """UPDATE configs SET status='AVAILABLE',reserved_by=NULL,reserved_until=NULL
                   WHERE status='RESERVED' AND reserved_until IS NOT NULL AND reserved_until<%s""",
                (now_iso(),),
            )
        except Exception:
            log.exception("Maintenance failed")
        await asyncio.sleep(60)


async def polling_or_webhook():
    await on_startup()
    runner = await run_web_server()
    maintenance = asyncio.create_task(maintenance_loop())

    try:
        if WEBHOOK_BASE_URL:
            while True:
                await asyncio.sleep(3600)
        else:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        maintenance.cancel()
        await runner.cleanup()
        await on_shutdown()


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

async def main():
    await polling_or_webhook()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
