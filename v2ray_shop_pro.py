#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2Ray Shop PRO — single-file advanced Telegram shop bot.

Install:
    pip install aiogram==3.31.0 aiosqlite aiohttp

Required environment variables:
    BOT_TOKEN=...
    ADMIN_IDS=123456789,987654321

Optional environment variables:
    SHOP_NAME=V2RAY SHOP PRO
    CURRENCY=تومان
    CARD_NUMBER=6037-....
    CARD_OWNER=...
    PAYMENT_GATEWAY_URL=https://pay.example/checkout?order_id={order_id}&amount={amount}
    REFERRAL_REWARD=20000
    ORDER_TTL_MINUTES=30
    LOW_STOCK_THRESHOLD=3
    DATABASE_PATH=v2ray_shop_pro.db
    SUPPORT_USERNAME=my_support
    PORT=8080
    WEBHOOK_BASE_URL=https://example.com
    WEBHOOK_SECRET=telegram-webhook-secret

Notes:
- SQLite is used so the entire application can run from one source file.
- The online gateway variable is only a redirect/template integration point.
  Provider-specific API + signed callback verification should be added for a real gateway.
- Payment receipts are manual and require admin review.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

# ----------------------------- configuration --------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SHOP_NAME = os.getenv("SHOP_NAME", "V2RAY SHOP PRO").strip()
CURRENCY = os.getenv("CURRENCY", "تومان").strip()
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000-0000-0000-0000").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "نام صاحب کارت").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
PAYMENT_GATEWAY_URL = os.getenv("PAYMENT_GATEWAY_URL", "").strip()
REFERRAL_REWARD = max(0, int(os.getenv("REFERRAL_REWARD", "20000")))
ORDER_TTL_MINUTES = max(5, int(os.getenv("ORDER_TTL_MINUTES", "30")))
LOW_STOCK_THRESHOLD = max(0, int(os.getenv("LOW_STOCK_THRESHOLD", "3")))
DATABASE_PATH = os.getenv("DATABASE_PATH", "v2ray_shop_pro.db").strip()
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "telegram-webhook").strip("/") or "telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is required")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("v2ray-shop-pro")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router(name="v2ray-shop-pro")
dp.include_router(router)

DB_LOCK = asyncio.Lock()
BACKGROUND: list[asyncio.Task] = []

# ------------------------------- database -----------------------------------

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 telegram_id INTEGER UNIQUE NOT NULL,
 username TEXT DEFAULT '', first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
 language TEXT DEFAULT 'fa', is_blocked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_seen ON users(last_seen_at);

CREATE TABLE IF NOT EXISTS admins(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL DEFAULT 'OWNER',
 permissions_json TEXT NOT NULL DEFAULT '{"all":true}',
 is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
 description TEXT DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
 sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
 name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, description TEXT DEFAULT '',
 protocol TEXT NOT NULL DEFAULT 'VLESS', price INTEGER NOT NULL CHECK(price>=0),
 currency TEXT NOT NULL DEFAULT 'تومان', duration_days INTEGER NOT NULL DEFAULT 30,
 traffic_gb INTEGER, device_limit INTEGER,
 sort_order INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active,sort_order);

CREATE TABLE IF NOT EXISTS configs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 config_text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'AVAILABLE',
 reserved_by_order_id INTEGER,
 reserved_until TEXT,
 sold_at TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_configs_stock ON configs(product_id,status);
CREATE INDEX IF NOT EXISTS idx_configs_reserved ON configs(reserved_by_order_id,status);

CREATE TABLE IF NOT EXISTS carts(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 quantity INTEGER NOT NULL CHECK(quantity>0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,product_id)
);

CREATE TABLE IF NOT EXISTS orders(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_number TEXT UNIQUE NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id),
 status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT',
 subtotal INTEGER NOT NULL DEFAULT 0,
 discount_amount INTEGER NOT NULL DEFAULT 0,
 wallet_amount INTEGER NOT NULL DEFAULT 0,
 payable_amount INTEGER NOT NULL DEFAULT 0,
 currency TEXT NOT NULL DEFAULT 'تومان',
 discount_id INTEGER,
 payment_status TEXT NOT NULL DEFAULT 'PENDING',
 expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(status,expires_at);

CREATE TABLE IF NOT EXISTS order_items(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id),
 quantity INTEGER NOT NULL CHECK(quantity>0),
 unit_price INTEGER NOT NULL,
 total_price INTEGER NOT NULL,
 created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_configs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 config_id INTEGER NOT NULL REFERENCES configs(id),
 created_at TEXT NOT NULL,
 UNIQUE(order_id,config_id)
);

CREATE TABLE IF NOT EXISTS payments(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id),
 method TEXT NOT NULL,
 amount INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 gateway TEXT, gateway_transaction_id TEXT,
 receipt_file_id TEXT, receipt_type TEXT, receipt_caption TEXT,
 reviewed_at TEXT, reviewed_by INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status,created_at DESC);

CREATE TABLE IF NOT EXISTS discounts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 code TEXT UNIQUE NOT NULL,
 type TEXT NOT NULL CHECK(type IN ('PERCENT','FIXED')),
 value INTEGER NOT NULL CHECK(value>=0),
 minimum_order_amount INTEGER NOT NULL DEFAULT 0,
 maximum_discount INTEGER,
 usage_limit INTEGER,
 usage_count INTEGER NOT NULL DEFAULT 0,
 per_user_limit INTEGER NOT NULL DEFAULT 1,
 starts_at TEXT, expires_at TEXT,
 is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discount_usages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 discount_id INTEGER NOT NULL REFERENCES discounts(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 amount INTEGER NOT NULL, used_at TEXT NOT NULL,
 UNIQUE(discount_id,order_id)
);

CREATE TABLE IF NOT EXISTS wallets(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
 currency TEXT NOT NULL DEFAULT 'تومان', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_transactions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 type TEXT NOT NULL, amount INTEGER NOT NULL,
 balance_before INTEGER NOT NULL, balance_after INTEGER NOT NULL,
 reference_type TEXT, reference_id TEXT, description TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_deposits(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 amount INTEGER NOT NULL CHECK(amount>0), status TEXT NOT NULL DEFAULT 'PENDING',
 receipt_file_id TEXT, receipt_type TEXT, caption TEXT,
 reviewed_by INTEGER, reviewed_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 referrer_user_id INTEGER NOT NULL REFERENCES users(id),
 referred_user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
 code TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 reward_amount INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, completed_at TEXT
);

CREATE TABLE IF NOT EXISTS tickets(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_number TEXT UNIQUE NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id),
 category TEXT NOT NULL DEFAULT 'OTHER', subject TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'OPEN', priority TEXT NOT NULL DEFAULT 'NORMAL',
 assigned_admin_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS ticket_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
 sender_type TEXT NOT NULL, sender_id INTEGER NOT NULL,
 message TEXT NOT NULL, telegram_message_id INTEGER, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,product_id)
);

CREATE TABLE IF NOT EXISTS broadcasts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 admin_id INTEGER NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 total_users INTEGER NOT NULL DEFAULT 0, sent_count INTEGER NOT NULL DEFAULT 0,
 failed_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, completed_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 admin_id INTEGER NOT NULL, action TEXT NOT NULL,
 target_type TEXT DEFAULT '', target_id TEXT DEFAULT '', details TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_logs ON admin_logs(created_at DESC);
"""

DEFAULT_CATEGORIES = [
    ("VLESS", "vless", "محصولات VLESS"),
    ("Trojan", "trojan", "محصولات Trojan"),
    ("VMess", "vmess", "محصولات VMess"),
    ("Shadowsocks", "shadowsocks", "محصولات Shadowsocks"),
    ("سایر", "other", "سایر محصولات"),
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> str:
    return now().isoformat(timespec="seconds")


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def esc(value: Any) -> str:
    return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def token(prefix: str = "") -> str:
    return prefix + secrets.token_hex(5).upper()


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def back(target: str = "home") -> InlineKeyboardMarkup:
    return kb([[('🔙 بازگشت', target)]])


def status_text(status: str) -> str:
    return {
        'PENDING_PAYMENT': '💳 در انتظار پرداخت', 'AWAITING_RECEIPT': '📎 منتظر رسید',
        'PAYMENT_REVIEW': '🔎 در بررسی پرداخت', 'PAID': '✅ پرداخت شده',
        'COMPLETED': '🎉 تکمیل شده', 'REJECTED': '❌ رد شده', 'CANCELLED': '🚫 لغو شده',
        'EXPIRED': '⌛ منقضی', 'OPEN': '🟢 باز', 'WAITING': '🟡 منتظر پاسخ', 'CLOSED': '⚫ بسته'
    }.get(status, status)


def parse_amount(text: str) -> int:
    clean = text.replace(',', '').replace('٬', '').strip()
    if not re.fullmatch(r'\d+', clean):
        raise ValueError
    value = int(clean)
    if value <= 0:
        raise ValueError
    return value


def chunks(text: str, max_len: int = 3800):
    while len(text) > max_len:
        cut = text.rfind('\n', 0, max_len)
        if cut < 500:
            cut = max_len
        yield text[:cut]
        text = text[cut:].lstrip('\n')
    if text:
        yield text


def payment_url(order_id: int, amount: int) -> str:
    return PAYMENT_GATEWAY_URL.replace('{order_id}', str(order_id)).replace('{amount}', str(amount)) if PAYMENT_GATEWAY_URL else ''


@asynccontextmanager
async def conn():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('PRAGMA busy_timeout=5000')
        yield db


async def init_db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with conn() as db:
        await db.executescript(SCHEMA)
        for name, slug, desc in DEFAULT_CATEGORIES:
            await db.execute(
                'INSERT OR IGNORE INTO categories(name,slug,description,created_at) VALUES(?,?,?,?)',
                (name, slug, desc, ts()),
            )
        await db.commit()


async def execute(sql: str, params: tuple = ()):
    async with DB_LOCK:
        async with conn() as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur


async def one(sql: str, params: tuple = ()):
    async with conn() as db:
        return await (await db.execute(sql, params)).fetchone()


async def rows(sql: str, params: tuple = ()):
    async with conn() as db:
        return await (await db.execute(sql, params)).fetchall()


async def scalar(sql: str, params: tuple = ()):
    row = await one(sql, params)
    return row[0] if row else None


async def audit(admin_id: int, action: str, target_type='', target_id='', details=''):
    await execute(
        'INSERT INTO admin_logs(admin_id,action,target_type,target_id,details,created_at) VALUES(?,?,?,?,?,?)',
        (admin_id, action, target_type, str(target_id), str(details)[:2000], ts()),
    )


async def ensure_user(tg) -> dict:
    stamp = ts()
    async with DB_LOCK:
        async with conn() as db:
            await db.execute(
                '''INSERT INTO users(telegram_id,username,first_name,last_name,language,created_at,updated_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                   username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
                   language=excluded.language, updated_at=excluded.updated_at, last_seen_at=excluded.last_seen_at''',
                (tg.id, tg.username or '', tg.first_name or '', tg.last_name or '', tg.language_code or 'fa', stamp, stamp, stamp),
            )
            user = await (await db.execute('SELECT * FROM users WHERE telegram_id=?', (tg.id,))).fetchone()
            await db.execute(
                'INSERT OR IGNORE INTO wallets(user_id,currency,updated_at) VALUES(?,?,?)',
                (user['id'], CURRENCY, stamp),
            )
            if tg.id in ADMIN_IDS:
                await db.execute(
                    '''INSERT INTO admins(user_id,role,permissions_json,is_active,created_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET is_active=1,role='OWNER',permissions_json='{"all":true}' ''',
                    (user['id'], 'OWNER', '{"all":true}', 1, stamp),
                )
            await db.commit()
            return dict(user)

# ------------------------------------ states ----------------------------------

class ProductState(StatesGroup):
    name = State(); category = State(); protocol = State(); price = State()
    duration = State(); traffic = State(); devices = State(); description = State()

class ConfigState(StatesGroup):
    product_id = State(); payload = State()

class DiscountState(StatesGroup):
    code = State(); typ = State(); value = State(); minimum = State(); maximum = State(); limit = State(); expires = State()

class ApplyCouponState(StatesGroup):
    order_id = State()

class TicketState(StatesGroup):
    category = State(); subject = State(); body = State()

class TicketReplyState(StatesGroup):
    ticket_id = State(); body = State()

class WalletState(StatesGroup):
    amount = State()

class SearchState(StatesGroup):
    query = State()

class UserLookupState(StatesGroup):
    telegram_id = State()

class BroadcastState(StatesGroup):
    body = State()

# ----------------------------------- UI --------------------------------------

def home_kb(admin=False):
    result = [
        [('🛍 فروشگاه', 'shop'), ('🛒 سبد خرید', 'cart')],
        [('📦 سفارش‌ها', 'orders'), ('👤 حساب', 'account')],
        [('💰 کیف پول', 'wallet'), ('🎁 دعوت دوستان', 'referral')],
        [('❤️ علاقه‌مندی‌ها', 'favorites'), ('🎟 تخفیف', 'discount')],
        [('🆘 پشتیبانی', 'support'), ('📚 راهنما', 'guide')],
    ]
    if admin:
        result.append([('⚙️ پنل مدیریت', 'admin')])
    return kb(result)


def admin_kb():
    return kb([
        [('📊 داشبورد', 'a_dashboard'), ('🛍 محصولات', 'a_products')],
        [('🔐 موجودی', 'a_inventory'), ('🧾 سفارش‌ها', 'a_orders')],
        [('💳 پرداخت‌ها', 'a_payments'), ('💰 کیف پول', 'a_wallet')],
        [('🎟 تخفیف‌ها', 'a_discounts'), ('🎁 دعوت‌ها', 'a_referrals')],
        [('🆘 تیکت‌ها', 'a_tickets'), ('📢 پیام همگانی', 'a_broadcast')],
        [('👥 کاربران', 'a_users'), ('📈 گزارش', 'a_stats')],
        [('⚙️ تنظیمات', 'a_settings'), ('🧾 لاگ‌ها', 'a_logs')],
        [('🔙 خانه', 'home')],
    ])


async def edit(call: CallbackQuery, text: str, markup: Optional[InlineKeyboardMarkup] = None):
    try:
        if call.message:
            await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        if call.message:
            await call.message.answer(text, reply_markup=markup)


async def send_home(message: Message):
    u = await ensure_user(message.from_user)
    if u['is_blocked']:
        return await message.answer('⛔ دسترسی حساب شما مسدود است.')
    balance = await scalar('SELECT balance FROM wallets WHERE user_id=?', (u['id'],)) or 0
    cart_count = await scalar('SELECT COALESCE(SUM(quantity),0) FROM carts WHERE user_id=?', (u['id'],)) or 0
    active = await scalar("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('PENDING_PAYMENT','AWAITING_RECEIPT','PAYMENT_REVIEW')", (u['id'],)) or 0
    text = (
        f'<b>✨ {esc(SHOP_NAME)}</b>\n\n'
        'فروشگاه حرفه‌ای سرویس‌های کانفیگ\n\n'
        '🔐 VLESS • Trojan • VMess • Shadowsocks\n'
        '⚡ تحویل سریع پس از تأیید پرداخت\n'
        '🎟 تخفیف + کیف پول + دعوت دوستان\n'
        '🆘 پشتیبانی تیکتی\n\n'
        f'💰 موجودی: <b>{money(balance)} {esc(CURRENCY)}</b>\n'
        f'🛒 سبد: <b>{cart_count}</b> آیتم\n'
        f'⏳ سفارش فعال: <b>{active}</b>'
    )
    await message.answer(text, reply_markup=home_kb(is_admin(message.from_user.id)))

# -------------------------------- core user ----------------------------------

@router.message(CommandStart())
async def start(message: Message):
    u = await ensure_user(message.from_user)
    if u['is_blocked']:
        return await message.answer('⛔ دسترسی حساب شما مسدود است.')
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith('ref_'):
        await register_referral(message.from_user.id, parts[1][4:])
    await send_home(message)


@router.message(Command('menu'))
async def menu(message: Message):
    await send_home(message)


@router.message(Command('id'))
async def user_id(message: Message):
    await message.answer(f'🆔 شناسه شما: <code>{message.from_user.id}</code>')


@router.message(Command('admin'))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer('⛔ دسترسی ندارید.')
    await admin_home(message)


@router.callback_query(F.data == 'noop')
async def noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == 'home')
async def home(call: CallbackQuery):
    await call.answer()
    await edit(call, f'<b>✨ {esc(SHOP_NAME)}</b>\n\nبه فروشگاه خوش آمدید 👋', home_kb(is_admin(call.from_user.id)))


@router.callback_query(F.data == 'shop')
async def shop(call: CallbackQuery):
    await call.answer()
    cats = await rows('SELECT * FROM categories WHERE is_active=1 ORDER BY sort_order,id')
    buttons = [[(f'🔹 {c["name"]}', f'cat:{c["id"]}')] for c in cats]
    buttons += [[('🔥 همه محصولات', 'all_products')], [('🔎 جستجو', 'search_products'), ('❤️ علاقه‌مندی‌ها', 'favorites')], [('🔙 خانه', 'home')]]
    await edit(call, '<b>🛍 فروشگاه</b>\n\nدسته‌بندی را انتخاب کنید:', kb(buttons))


async def product_rows(condition='1=1', params=()):
    return await rows(
        f'''SELECT p.*, c.name AS category_name,
            (SELECT COUNT(*) FROM configs c2 WHERE c2.product_id=p.id AND c2.status='AVAILABLE') stock,
            (SELECT COUNT(*) FROM configs c3 WHERE c3.product_id=p.id AND c3.status='RESERVED') reserved
            FROM products p LEFT JOIN categories c ON c.id=p.category_id
            WHERE p.is_active=1 AND {condition}
            ORDER BY p.sort_order,p.id DESC LIMIT 60''', params)


async def show_products(call: CallbackQuery, products, title='محصولات', back_target='shop'):
    if not products:
        return await edit(call, '📭 محصولی پیدا نشد.', back(back_target))
    buttons = [[(f'{"🟢" if p["stock"] else "🔴"} {p["name"]} | {money(p["price"])} {CURRENCY}', f'product:{p["id"]}')] for p in products]
    buttons.append([('🔙 بازگشت', back_target)])
    await edit(call, f'<b>{esc(title)}</b>\n\nمحصول را انتخاب کنید:', kb(buttons))


@router.callback_query(F.data == 'all_products')
async def all_products(call: CallbackQuery):
    await call.answer(); await show_products(call, await product_rows(), '🔥 همه محصولات')


@router.callback_query(F.data.startswith('cat:'))
async def category(call: CallbackQuery):
    await call.answer(); cid = int(call.data.split(':', 1)[1])
    cat = await one('SELECT name FROM categories WHERE id=? AND is_active=1', (cid,))
    await show_products(call, await product_rows('p.category_id=?', (cid,)), f'🔹 {cat["name"]}' if cat else 'محصولات')


async def product_detail(pid: int, uid: int, qty: int = 1):
    p = await one('''SELECT p.*, c.name category_name,
                     (SELECT COUNT(*) FROM configs x WHERE x.product_id=p.id AND x.status='AVAILABLE') stock,
                     (SELECT COUNT(*) FROM configs y WHERE y.product_id=p.id AND y.status='RESERVED') reserved
                     FROM products p LEFT JOIN categories c ON c.id=p.category_id
                     WHERE p.id=? AND p.is_active=1''', (pid,))
    if not p:
        return None
    fav = bool(await scalar('SELECT 1 FROM favorites WHERE user_id=? AND product_id=?', (uid, pid)))
    traffic = 'نامحدود' if p['traffic_gb'] is None else f'{p["traffic_gb"]} GB'
    devices = 'نامحدود' if p['device_limit'] is None else p['device_limit']
    qty = max(1, min(int(qty), max(1, int(p['stock']))))
    text = (
        f'<b>{esc(p["name"])}</b>\n\n━━━━━━━━━━━━━━━━━━\n'
        f'📁 دسته: {esc(p["category_name"] or "-")}\n'
        f'📌 پروتکل: <b>{esc(p["protocol"])}</b>\n'
        f'⏳ اعتبار: {p["duration_days"]} روز\n📊 حجم: {traffic}\n📱 دستگاه: {devices}\n'
        f'📦 موجود: {p["stock"]} | رزرو: {p["reserved"]}\n'
        f'💰 قیمت واحد: <b>{money(p["price"])} {esc(CURRENCY)}</b>\n'
        f'🧮 مبلغ انتخابی: <b>{money(p["price"]*qty)} {esc(CURRENCY)}</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n' + esc(p['description'] or 'توضیحی ثبت نشده است.')
    )
    buttons = [
        [('➖', f'qty_dec:{pid}'), (f'تعداد: {qty}', 'noop'), ('➕', f'qty_inc:{pid}')],
        [('🛒 افزودن به سبد', f'cart_add:{pid}:{qty}')],
        [('❤️ حذف از علاقه‌مندی' if fav else '🤍 افزودن به علاقه‌مندی', f'fav_toggle:{pid}')],
        [('🔙 محصولات', 'shop')]
    ]
    return text, kb(buttons)


@router.callback_query(F.data.startswith('product:'))
async def product(call: CallbackQuery, state: FSMContext):
    await call.answer()
    pid = int(call.data.split(':', 1)[1]); data = await state.get_data(); q = int(data.get(f'qty_{pid}', 1))
    result = await product_detail(pid, call.from_user.id, q)
    if not result:
        return await edit(call, '❌ محصول پیدا نشد.', back('shop'))
    await edit(call, result[0], result[1])


async def change_product_qty(call: CallbackQuery, state: FSMContext, delta: int):
    pid = int(call.data.split(':', 1)[1]); data = await state.get_data(); q = max(1, int(data.get(f'qty_{pid}', 1)) + delta)
    stock = int(await scalar("SELECT COUNT(*) FROM configs WHERE product_id=? AND status='AVAILABLE'", (pid,)) or 0)
    q = min(q, max(1, stock), 20); await state.update_data(**{f'qty_{pid}': q})
    result = await product_detail(pid, call.from_user.id, q)
    if result:
        await call.message.edit_text(result[0], reply_markup=result[1])


@router.callback_query(F.data.startswith('qty_inc:'))
async def qty_inc(call: CallbackQuery, state: FSMContext):
    await call.answer(); await change_product_qty(call, state, 1)


@router.callback_query(F.data.startswith('qty_dec:'))
async def qty_dec(call: CallbackQuery, state: FSMContext):
    await call.answer(); await change_product_qty(call, state, -1)


@router.callback_query(F.data.startswith('cart_add:'))
async def cart_add(call: CallbackQuery):
    await call.answer()
    pieces = call.data.split(':'); pid = int(pieces[1]); qty = max(1, int(pieces[2]))
    uid = (await ensure_user(call.from_user))['id']
    stock = int(await scalar("SELECT COUNT(*) FROM configs WHERE product_id=? AND status='AVAILABLE'", (pid,)) or 0)
    if stock <= 0:
        return await call.answer('⛔ ناموجود است.', show_alert=True)
    current = int(await scalar('SELECT quantity FROM carts WHERE user_id=? AND product_id=?', (uid, pid)) or 0)
    new_qty = min(20, stock, current + qty)
    await execute('''INSERT INTO carts(user_id,product_id,quantity,created_at,updated_at) VALUES(?,?,?,?,?)
                     ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at''',
                  (uid, pid, new_qty, ts(), ts()))
    await call.message.edit_text(f'✅ {qty} عدد به سبد اضافه شد.\nتعداد فعلی: <b>{new_qty}</b>', reply_markup=kb([[('🛒 مشاهده سبد', 'cart')], [('🛍 ادامه خرید', 'shop')]]))


@router.callback_query(F.data.startswith('fav_toggle:'))
async def fav_toggle(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; pid = int(call.data.split(':', 1)[1])
    existing = await scalar('SELECT 1 FROM favorites WHERE user_id=? AND product_id=?', (uid, pid))
    if existing:
        await execute('DELETE FROM favorites WHERE user_id=? AND product_id=?', (uid, pid))
    else:
        await execute('INSERT OR IGNORE INTO favorites(user_id,product_id,created_at) VALUES(?,?,?)', (uid, pid, ts()))
    result = await product_detail(pid, call.from_user.id)
    if result: await edit(call, result[0], result[1])


@router.callback_query(F.data == 'favorites')
async def favorites(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']
    items = await rows('''SELECT p.*,(SELECT COUNT(*) FROM configs c WHERE c.product_id=p.id AND c.status='AVAILABLE') stock
                          FROM favorites f JOIN products p ON p.id=f.product_id WHERE f.user_id=? AND p.is_active=1
                          ORDER BY f.created_at DESC''', (uid,))
    await show_products(call, items, '❤️ علاقه‌مندی‌ها', 'home')


@router.callback_query(F.data == 'search_products')
async def search_products_start(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(SearchState.query); await call.message.answer('🔎 نام محصول، توضیح یا پروتکل را بفرستید:')


@router.message(SearchState.query)
async def search_products_do(message: Message, state: FSMContext):
    query = (message.text or '').strip()[:100]; await state.clear()
    if not query: return await message.answer('❌ عبارت جستجو خالی است.')
    like = f'%{query}%'
    items = await product_rows('(p.name LIKE ? OR p.protocol LIKE ? OR p.description LIKE ?)', (like, like, like))
    buttons = [[(f'{"🟢" if p["stock"] else "🔴"} {p["name"]} | {money(p["price"])} {CURRENCY}', f'product:{p["id"]}')] for p in items]
    buttons.append([('🔙 فروشگاه', 'shop')])
    await message.answer(f'<b>🔎 نتایج:</b> {esc(query)}', reply_markup=kb(buttons))

# ------------------------------- cart/order -----------------------------------

async def get_cart(uid: int):
    return await rows('''SELECT c.product_id,c.quantity,p.name,p.price,
                         (SELECT COUNT(*) FROM configs x WHERE x.product_id=p.id AND x.status='AVAILABLE') stock
                         FROM carts c JOIN products p ON p.id=c.product_id
                         WHERE c.user_id=? AND p.is_active=1 ORDER BY c.created_at''', (uid,))


async def render_cart(uid: int):
    items = await get_cart(uid)
    if not items:
        return '<b>🛒 سبد خرید</b>\n\nسبد شما خالی است.', kb([[('🛍 فروشگاه', 'shop')], [('🔙 خانه', 'home')]])
    total = 0; text = '<b>🛒 سبد خرید</b>\n\n'; buttons = []
    for item in items:
        q = min(int(item['quantity']), int(item['stock'])); line = int(item['price']) * q; total += line
        note = '' if q == item['quantity'] else f' ⚠️ موجودی {item["stock"]}'
        text += f'• {esc(item["name"])} × {q} = <b>{money(line)}</b> {CURRENCY}{note}\n'
        buttons.append([(f'✏️ {item["name"]} × {q}', f'cart_item:{item["product_id"]}')])
    text += f'\n💰 جمع: <b>{money(total)} {CURRENCY}</b>'
    buttons += [[('✅ ادامه پرداخت', 'checkout')], [('🗑 خالی کردن', 'cart_clear')], [('🔙 خانه', 'home')]]
    return text, kb(buttons)


@router.callback_query(F.data == 'cart')
async def cart(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; text, markup = await render_cart(uid); await edit(call, text, markup)


@router.callback_query(F.data.startswith('cart_item:'))
async def cart_item(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; pid = int(call.data.split(':', 1)[1])
    item = await one('''SELECT c.quantity,p.name,(SELECT COUNT(*) FROM configs x WHERE x.product_id=c.product_id AND x.status='AVAILABLE') stock
                        FROM carts c JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND c.product_id=?''', (uid, pid))
    if not item: return await cart(call)
    await edit(call, f'<b>✏️ {esc(item["name"])}</b>\n\nتعداد: <b>{item["quantity"]}</b>\nموجود: <b>{item["stock"]}</b>',
               kb([[('➖', f'cart_dec:{pid}'), (str(item['quantity']), 'noop'), ('➕', f'cart_inc:{pid}')],
                   [('🗑 حذف', f'cart_remove:{pid}')], [('🔙 سبد', 'cart')]]))


async def mutate_cart(uid: int, pid: int, delta: int):
    item = await one('''SELECT quantity,(SELECT COUNT(*) FROM configs x WHERE x.product_id=c.product_id AND x.status='AVAILABLE') stock
                        FROM carts c WHERE user_id=? AND product_id=?''', (uid, pid))
    if not item: return
    q = max(0, min(20, int(item['quantity']) + delta, int(item['stock'])))
    if q:
        await execute('UPDATE carts SET quantity=?,updated_at=? WHERE user_id=? AND product_id=?', (q, ts(), uid, pid))
    else:
        await execute('DELETE FROM carts WHERE user_id=? AND product_id=?', (uid, pid))


@router.callback_query(F.data.startswith('cart_inc:'))
async def cart_inc(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; await mutate_cart(uid, int(call.data.split(':', 1)[1]), 1); await cart(call)


@router.callback_query(F.data.startswith('cart_dec:'))
async def cart_dec(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; await mutate_cart(uid, int(call.data.split(':', 1)[1]), -1); await cart(call)


@router.callback_query(F.data.startswith('cart_remove:'))
async def cart_remove(call: CallbackQuery):
    await call.answer('حذف شد'); uid = (await ensure_user(call.from_user))['id']
    await execute('DELETE FROM carts WHERE user_id=? AND product_id=?', (uid, int(call.data.split(':', 1)[1]))); await cart(call)


@router.callback_query(F.data == 'cart_clear')
async def cart_clear(call: CallbackQuery):
    await call.answer('سبد پاک شد'); uid = (await ensure_user(call.from_user))['id']; await execute('DELETE FROM carts WHERE user_id=?', (uid,)); await cart(call)


async def create_order(uid: int):
    """Create order and reserve exact config records atomically."""
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            try:
                items = await (await db.execute('''SELECT c.product_id,c.quantity,p.name,p.price,
                    (SELECT COUNT(*) FROM configs x WHERE x.product_id=p.id AND x.status='AVAILABLE') stock
                    FROM carts c JOIN products p ON p.id=c.product_id
                    WHERE c.user_id=? AND p.is_active=1''', (uid,))).fetchall()
                if not items:
                    await db.rollback(); return None, 'سبد خالی است.'
                subtotal = 0
                for item in items:
                    if item['stock'] < item['quantity']:
                        await db.rollback(); return None, f'موجودی «{item["name"]}» کافی نیست.'
                    subtotal += int(item['price']) * int(item['quantity'])
                created = ts(); expires = (now() + timedelta(minutes=ORDER_TTL_MINUTES)).isoformat(timespec='seconds')
                order_number = token('V2-')
                cur = await db.execute('''INSERT INTO orders(order_number,user_id,status,subtotal,payable_amount,currency,expires_at,created_at,updated_at)
                                          VALUES(?,?,?,?,?,?,?,?,?)''',
                                       (order_number, uid, 'PENDING_PAYMENT', subtotal, subtotal, CURRENCY, expires, created, created))
                oid = cur.lastrowid
                for item in items:
                    await db.execute('''INSERT INTO order_items(order_id,product_id,quantity,unit_price,total_price,created_at)
                                        VALUES(?,?,?,?,?,?)''', (oid, item['product_id'], item['quantity'], item['price'], item['price'] * item['quantity'], created))
                    configs = await (await db.execute('''SELECT id FROM configs WHERE product_id=? AND status='AVAILABLE' ORDER BY id LIMIT ?''',
                                                       (item['product_id'], item['quantity']))).fetchall()
                    if len(configs) < item['quantity']:
                        await db.rollback(); return None, f'موجودی لحظه‌ای «{item["name"]}» کافی نیست.'
                    for c in configs:
                        cur2 = await db.execute('''UPDATE configs SET status='RESERVED',reserved_by_order_id=?,reserved_until=?
                                                   WHERE id=? AND status='AVAILABLE' ''', (oid, expires, c['id']))
                        if cur2.rowcount != 1:
                            await db.rollback(); return None, 'رزرو موجودی ناموفق بود؛ دوباره تلاش کنید.'
                await db.execute('DELETE FROM carts WHERE user_id=?', (uid,))
                await db.commit()
                return {'id': oid, 'order_number': order_number}, None
            except Exception:
                await db.rollback(); raise


async def order_payment_kb(oid: int):
    options = []
    if PAYMENT_GATEWAY_URL:
        options.append([('🌐 پرداخت آنلاین', f'gateway:{oid}')])
    options += [[('💳 کارت‌به‌کارت', f'cardpay:{oid}'), ('💰 کیف پول', f'walletpay:{oid}')],
                [('🎟 کد تخفیف', f'coupon:{oid}')], [('❌ لغو سفارش', f'cancel_order:{oid}')]]
    return kb(options)


async def pay_text(order):
    return (f'<b>💳 پرداخت سفارش</b>\n\n🧾 <code>{esc(order["order_number"])}</code>\n'
            f'💰 مبلغ: <b>{money(order["payable_amount"])} {esc(CURRENCY)}</b>\n'
            f'🏷 تخفیف: {money(order["discount_amount"])} {esc(CURRENCY)}\n'
            f'⏰ انقضا: <code>{esc(order["expires_at"])}</code>\n\nروش پرداخت را انتخاب کنید.')


@router.callback_query(F.data == 'checkout')
async def checkout(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']; result, error = await create_order(uid)
    if not result:
        return await edit(call, f'❌ {esc(error)}', back('cart'))
    order = await one('SELECT * FROM orders WHERE id=?', (result['id'],))
    await edit(call, await pay_text(order), await order_payment_kb(order['id']))


async def coupon_check(code: str, uid: int, subtotal: int):
    discount = await one('SELECT * FROM discounts WHERE code=? AND is_active=1', (code.upper(),))
    if not discount: return None, '❌ کد تخفیف معتبر نیست.'
    starts = parse_dt(discount['starts_at']); expires = parse_dt(discount['expires_at'])
    if starts and now() < starts: return None, '⏳ این کد هنوز فعال نشده.'
    if expires and now() > expires: return None, '⌛ اعتبار کد تمام شده.'
    if subtotal < discount['minimum_order_amount']: return None, f'حداقل مبلغ {money(discount["minimum_order_amount"])} {CURRENCY} است.'
    if discount['usage_limit'] is not None and discount['usage_count'] >= discount['usage_limit']: return None, '⚠️ ظرفیت استفاده تمام شده.'
    used = await scalar('SELECT COUNT(*) FROM discount_usages WHERE discount_id=? AND user_id=?', (discount['id'], uid)) or 0
    if used >= discount['per_user_limit']: return None, 'این کد به سقف استفاده شما رسیده.'
    amount = subtotal * discount['value'] // 100 if discount['type'] == 'PERCENT' else discount['value']
    if discount['maximum_discount'] is not None: amount = min(amount, discount['maximum_discount'])
    return discount, max(0, min(amount, subtotal))


@router.callback_query(F.data.startswith('coupon:'))
async def coupon_start(call: CallbackQuery, state: FSMContext):
    await call.answer(); oid = int(call.data.split(':', 1)[1]); uid = (await ensure_user(call.from_user))['id']
    if not await scalar('SELECT 1 FROM orders WHERE id=? AND user_id=? AND status="PENDING_PAYMENT"', (oid, uid)):
        return await call.answer('سفارش قابل تغییر نیست.', show_alert=True)
    await state.update_data(order_id=oid); await state.set_state(ApplyCouponState.order_id); await call.message.answer('🎟 کد تخفیف را ارسال کنید:')


@router.message(ApplyCouponState.order_id)
async def coupon_apply(message: Message, state: FSMContext):
    data = await state.get_data(); await state.clear(); oid = int(data['order_id']); uid = (await ensure_user(message.from_user))['id']
    order = await one('SELECT * FROM orders WHERE id=? AND user_id=? AND status="PENDING_PAYMENT"', (oid, uid))
    if not order: return await message.answer('❌ سفارش پیدا نشد یا قابل ویرایش نیست.')
    discount, amount_or_error = await coupon_check((message.text or '').strip(), uid, order['subtotal'])
    if not discount: return await message.answer(str(amount_or_error), reply_markup=await order_payment_kb(oid))
    amount = int(amount_or_error)
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            old = await (await db.execute('SELECT discount_id FROM orders WHERE id=?', (oid,))).fetchone()
            if old and old['discount_id']:
                await db.execute('DELETE FROM discount_usages WHERE order_id=?', (oid,))
                await db.execute('UPDATE discounts SET usage_count=MAX(usage_count-1,0) WHERE id=?', (old['discount_id'],))
            await db.execute('INSERT INTO discount_usages(discount_id,user_id,order_id,amount,used_at) VALUES(?,?,?,?,?)', (discount['id'], uid, oid, amount, ts()))
            await db.execute('UPDATE discounts SET usage_count=usage_count+1 WHERE id=?', (discount['id'],))
            await db.execute('UPDATE orders SET discount_id=?,discount_amount=?,payable_amount=?,updated_at=? WHERE id=?',
                              (discount['id'], amount, order['subtotal'] - amount, ts(), oid))
            await db.commit()
    order = await one('SELECT * FROM orders WHERE id=?', (oid,))
    await message.answer('✅ کد تخفیف اعمال شد.\n\n' + await pay_text(order), reply_markup=await order_payment_kb(oid))


async def pending_payment_for(uid: int, oid: int):
    return await one("SELECT * FROM payments WHERE user_id=? AND order_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1", (uid, oid))


@router.callback_query(F.data.startswith('cardpay:'))
async def cardpay(call: CallbackQuery):
    await call.answer(); oid = int(call.data.split(':', 1)[1]); uid = (await ensure_user(call.from_user))['id']
    order = await one('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, uid))
    if not order or order['status'] not in ('PENDING_PAYMENT', 'AWAITING_RECEIPT'):
        return await call.answer('سفارش قابل پرداخت نیست.', show_alert=True)
    if not await pending_payment_for(uid, oid):
        await execute('''INSERT INTO payments(order_id,user_id,method,amount,status,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?)''', (oid, uid, 'CARD_TO_CARD', order['payable_amount'], 'PENDING', ts(), ts()))
    await execute('UPDATE orders SET status="AWAITING_RECEIPT",updated_at=? WHERE id=?', (ts(), oid))
    await call.message.edit_text(
        f'<b>💳 کارت‌به‌کارت</b>\n\nسفارش: <code>{esc(order["order_number"])}</code>\n'
        f'مبلغ دقیق: <b>{money(order["payable_amount"])} {esc(CURRENCY)}</b>\n\n'
        f'💳 کارت: <code>{esc(CARD_NUMBER)}</code>\n👤 به نام: <b>{esc(CARD_OWNER)}</b>\n\n'
        'رسید را عکس یا فایل ارسال کنید.',
        reply_markup=kb([[('❌ لغو سفارش', f'cancel_order:{oid}')], [('🔙 خانه', 'home')]])
    )


@router.callback_query(F.data.startswith('gateway:'))
async def gateway(call: CallbackQuery):
    await call.answer(); oid = int(call.data.split(':', 1)[1]); uid = (await ensure_user(call.from_user))['id']
    order = await one('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, uid))
    url = payment_url(oid, order['payable_amount']) if order else ''
    if not order or order['status'] != 'PENDING_PAYMENT': return await call.answer('سفارش قابل پرداخت نیست.', show_alert=True)
    if not url: return await call.answer('درگاه فعال نیست.', show_alert=True)
    if not await pending_payment_for(uid, oid):
        await execute('''INSERT INTO payments(order_id,user_id,method,amount,status,gateway,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?)''', (oid, uid, 'ONLINE_GATEWAY', order['payable_amount'], 'PENDING', url, ts(), ts()))
    await call.message.edit_text(
        f'<b>🌐 پرداخت آنلاین</b>\n\nسفارش: <code>{esc(order["order_number"])}</code>\nمبلغ: <b>{money(order["payable_amount"])} {CURRENCY}</b>',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🌐 صفحه پرداخت', url=url)], [InlineKeyboardButton(text='🔙 خانه', callback_data='home')]])
    )


async def claim_reserved_configs(oid: int) -> list[str]:
    """Atomically convert order reservations into SOLD rows and mark order complete."""
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            order = await (await db.execute('SELECT * FROM orders WHERE id=?', (oid,))).fetchone()
            if not order:
                await db.rollback(); return []
            items = await (await db.execute('SELECT quantity FROM order_items WHERE order_id=?', (oid,))).fetchall()
            expected = sum(int(x['quantity']) for x in items)
            cfgs = await (await db.execute('''SELECT id,config_text FROM configs
                                              WHERE reserved_by_order_id=? AND status='RESERVED'
                                              ORDER BY id''', (oid,))).fetchall()
            if len(cfgs) != expected:
                await db.rollback(); return []
            result = []
            for cfg in cfgs:
                cur = await db.execute('''UPDATE configs SET status='SOLD',reserved_by_order_id=NULL,reserved_until=NULL,sold_at=?
                                          WHERE id=? AND status='RESERVED' AND reserved_by_order_id=?''', (ts(), cfg['id'], oid))
                if cur.rowcount != 1:
                    await db.rollback(); return []
                await db.execute('INSERT INTO order_configs(order_id,config_id,created_at) VALUES(?,?,?)', (oid, cfg['id'], ts()))
                result.append(cfg['config_text'])
            await db.execute('UPDATE orders SET status="COMPLETED",payment_status="PAID",updated_at=? WHERE id=?', (ts(), oid))
            await db.commit()
            return result


async def release_order_reservations(oid: int):
    await execute('''UPDATE configs SET status='AVAILABLE',reserved_by_order_id=NULL,reserved_until=NULL
                     WHERE reserved_by_order_id=? AND status='RESERVED' ''', (oid,))


async def rollback_coupon_usage(oid: int):
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            usage = await (await db.execute('SELECT discount_id FROM discount_usages WHERE order_id=?', (oid,))).fetchone()
            if usage:
                await db.execute('UPDATE discounts SET usage_count=MAX(usage_count-1,0) WHERE id=?', (usage['discount_id'],))
                await db.execute('DELETE FROM discount_usages WHERE order_id=?', (oid,))
            await db.commit()


async def cancel_order_db(oid: int):
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            order = await (await db.execute('SELECT * FROM orders WHERE id=?', (oid,))).fetchone()
            if not order or order['status'] not in ('PENDING_PAYMENT', 'AWAITING_RECEIPT'):
                await db.rollback(); return False
            await db.execute('UPDATE configs SET status="AVAILABLE",reserved_by_order_id=NULL,reserved_until=NULL WHERE reserved_by_order_id=? AND status="RESERVED"', (oid,))
            usage = await (await db.execute('SELECT discount_id FROM discount_usages WHERE order_id=?', (oid,))).fetchone()
            if usage:
                await db.execute('UPDATE discounts SET usage_count=MAX(usage_count-1,0) WHERE id=?', (usage['discount_id'],))
                await db.execute('DELETE FROM discount_usages WHERE order_id=?', (oid,))
            await db.execute('UPDATE orders SET status="CANCELLED",updated_at=? WHERE id=?', (ts(), oid))
            await db.commit()
            return True


async def notify_delivery(oid: int, configs: list[str]):
    order = await one('''SELECT o.order_number,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?''', (oid,))
    if not order: return
    text = f'<b>🎉 سفارش {esc(order["order_number"])} تکمیل شد!</b>\n\n'
    for index, config_text in enumerate(configs, 1):
        text += f'<b>#{index}</b>\n<pre>{esc(config_text)}</pre>\n'
    for part in chunks(text):
        try: await bot.send_message(order['telegram_id'], part)
        except Exception: log.exception('delivery send failed')
    await complete_referral(await scalar('SELECT user_id FROM orders WHERE id=?', (oid,)))


@router.callback_query(F.data.startswith('walletpay:'))
async def walletpay(call: CallbackQuery):
    await call.answer(); oid = int(call.data.split(':', 1)[1]); uid = (await ensure_user(call.from_user))['id']
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            order = await (await db.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, uid))).fetchone()
            wallet = await (await db.execute('SELECT * FROM wallets WHERE user_id=?', (uid,))).fetchone()
            if not order or order['status'] != 'PENDING_PAYMENT':
                await db.rollback(); return await call.answer('سفارش قابل پرداخت نیست.', show_alert=True)
            expiry = parse_dt(order['expires_at'])
            if expiry and expiry < now():
                await db.rollback(); return await call.answer('⌛ سفارش منقضی شده.', show_alert=True)
            if wallet['balance'] < order['payable_amount']:
                await db.rollback(); return await call.answer('💰 موجودی کافی نیست.', show_alert=True)
            before = wallet['balance']; after = before - order['payable_amount']; stamp = ts()
            await db.execute('UPDATE wallets SET balance=?,updated_at=? WHERE user_id=?', (after, stamp, uid))
            await db.execute('''INSERT INTO wallet_transactions(user_id,type,amount,balance_before,balance_after,reference_type,reference_id,description,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)''', (uid,'PURCHASE',order['payable_amount'],before,after,'ORDER',str(oid),f'پرداخت سفارش {order["order_number"]}',stamp))
            await db.execute('''INSERT INTO payments(order_id,user_id,method,amount,status,reviewed_at,reviewed_by,created_at,updated_at)
                                VALUES(?,?,?,?,?,?,?,?,?)''', (oid,uid,'WALLET',order['payable_amount'],'VERIFIED',stamp,call.from_user.id,stamp,stamp))
            await db.execute('UPDATE orders SET status="PAID",payment_status="PAID",wallet_amount=?,updated_at=? WHERE id=?', (order['payable_amount'],stamp,oid))
            await db.commit()
    configs = await claim_reserved_configs(oid)
    if not configs:
        await refund_wallet(oid, 'عدم تطابق موجودی رزرو شده')
        await call.answer('⛔ تحویل ناموفق بود؛ مبلغ به کیف پول برگشت.', show_alert=True)
        return
    await notify_delivery(oid, configs)
    await call.message.edit_text('✅ پرداخت با کیف پول انجام شد و کانفیگ‌ها ارسال شدند.', reply_markup=back())


async def refund_wallet(oid: int, reason: str):
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            order = await (await db.execute('SELECT * FROM orders WHERE id=?', (oid,))).fetchone()
            if not order or order['wallet_amount'] <= 0:
                await db.rollback(); return
            wallet = await (await db.execute('SELECT * FROM wallets WHERE user_id=?', (order['user_id'],))).fetchone()
            before = wallet['balance']; after = before + order['wallet_amount']; stamp = ts()
            await db.execute('UPDATE wallets SET balance=?,updated_at=? WHERE user_id=?', (after,stamp,order['user_id']))
            await db.execute('''INSERT INTO wallet_transactions(user_id,type,amount,balance_before,balance_after,reference_type,reference_id,description,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)''', (order['user_id'],'REFUND',order['wallet_amount'],before,after,'ORDER',str(oid),reason,stamp))
            await db.execute('UPDATE orders SET status="REJECTED",payment_status="REFUNDED",updated_at=? WHERE id=?', (stamp,oid))
            await db.commit()
    await release_order_reservations(oid)


@router.callback_query(F.data.startswith('cancel_order:'))
async def cancel_order(call: CallbackQuery):
    await call.answer(); oid = int(call.data.split(':', 1)[1]); uid = (await ensure_user(call.from_user))['id']
    order = await one('SELECT status FROM orders WHERE id=? AND user_id=?', (oid, uid))
    if not order or order['status'] not in ('PENDING_PAYMENT','AWAITING_RECEIPT','PAYMENT_REVIEW'):
        return await call.answer('قابل لغو نیست.', show_alert=True)
    await cancel_order_db(oid); await call.message.edit_text('🚫 سفارش لغو شد.', reply_markup=back())

# -------------------------------- receipts -----------------------------------

async def notify_payment_admin(payment_id: int, uid: int, oid: int, amount: int, file_id: str, file_type: str):
    order = await one('SELECT order_number FROM orders WHERE id=?', (oid,)); user = await one('SELECT telegram_id,username FROM users WHERE id=?', (uid,))
    text = f'💳 <b>رسید پرداخت</b>\n\nپرداخت: <code>#{payment_id}</code>\nسفارش: <code>{esc(order["order_number"])}</code>\nکاربر: <code>{user["telegram_id"]}</code> @{esc(user["username"] or "-")}\nمبلغ: <b>{money(amount)} {CURRENCY}</b>'
    markup = kb([[('✅ تأیید', f'approve_payment:{payment_id}'), ('❌ رد', f'reject_payment:{payment_id}')]])
    for admin_id in ADMIN_IDS:
        try:
            if file_type == 'photo': await bot.send_photo(admin_id, file_id, caption=text, reply_markup=markup)
            else: await bot.send_document(admin_id, file_id, caption=text, reply_markup=markup)
        except Exception: log.exception('payment notify failed')


async def notify_deposit_admin(dep_id: int, uid: int, amount: int, file_id: str, file_type: str):
    user = await one('SELECT telegram_id,username FROM users WHERE id=?', (uid,))
    text = f'💰 <b>رسید شارژ کیف پول</b>\n\nدرخواست: <code>#{dep_id}</code>\nکاربر: <code>{user["telegram_id"]}</code> @{esc(user["username"] or "-")}\nمبلغ: <b>{money(amount)} {CURRENCY}</b>'
    markup = kb([[('✅ تأیید', f'approve_deposit:{dep_id}'), ('❌ رد', f'reject_deposit:{dep_id}')]])
    for admin_id in ADMIN_IDS:
        try:
            if file_type == 'photo': await bot.send_photo(admin_id, file_id, caption=text, reply_markup=markup)
            else: await bot.send_document(admin_id, file_id, caption=text, reply_markup=markup)
        except Exception: log.exception('deposit notify failed')


async def pending_card_payment(uid: int):
    return await one('''SELECT p.*,o.order_number FROM payments p JOIN orders o ON o.id=p.order_id
                        WHERE p.user_id=? AND p.status='PENDING' AND o.status='AWAITING_RECEIPT'
                        ORDER BY p.id DESC LIMIT 1''', (uid,))


async def pending_deposit(uid: int):
    return await one('SELECT * FROM wallet_deposits WHERE user_id=? AND status="PENDING" ORDER BY id DESC LIMIT 1', (uid,))


@router.message(F.photo)
async def photo_handler(message: Message):
    user = await ensure_user(message.from_user)
    if user['is_blocked']: return
    payment = await pending_card_payment(user['id'])
    if payment:
        file_id = message.photo[-1].file_id
        await execute('UPDATE payments SET receipt_file_id=?,receipt_type="photo",receipt_caption=?,updated_at=? WHERE id=?', (file_id,(message.caption or '')[:1000],ts(),payment['id']))
        await execute('UPDATE orders SET status="PAYMENT_REVIEW",updated_at=? WHERE id=?', (ts(),payment['order_id']))
        await notify_payment_admin(payment['id'],user['id'],payment['order_id'],payment['amount'],file_id,'photo')
        return await message.answer('✅ رسید پرداخت سفارش دریافت شد و برای بررسی ارسال گردید.')
    deposit = await pending_deposit(user['id'])
    if deposit:
        file_id = message.photo[-1].file_id
        await execute('UPDATE wallet_deposits SET receipt_file_id=?,receipt_type="photo",caption=?,updated_at=? WHERE id=?', (file_id,(message.caption or '')[:1000],ts(),deposit['id']))
        await notify_deposit_admin(deposit['id'],user['id'],deposit['amount'],file_id,'photo')
        return await message.answer('✅ رسید شارژ کیف پول دریافت شد.')
    await message.answer('📎 در حال حاضر رسیدی برای ثبت وجود ندارد.')


@router.message(F.document)
async def document_handler(message: Message):
    user = await ensure_user(message.from_user)
    if user['is_blocked']: return
    payment = await pending_card_payment(user['id'])
    if payment:
        file_id = message.document.file_id
        await execute('UPDATE payments SET receipt_file_id=?,receipt_type="document",receipt_caption=?,updated_at=? WHERE id=?', (file_id,(message.caption or '')[:1000],ts(),payment['id']))
        await execute('UPDATE orders SET status="PAYMENT_REVIEW",updated_at=? WHERE id=?', (ts(),payment['order_id']))
        await notify_payment_admin(payment['id'],user['id'],payment['order_id'],payment['amount'],file_id,'document')
        return await message.answer('✅ رسید پرداخت سفارش دریافت شد و برای بررسی ارسال گردید.')
    deposit = await pending_deposit(user['id'])
    if deposit:
        file_id = message.document.file_id
        await execute('UPDATE wallet_deposits SET receipt_file_id=?,receipt_type="document",caption=?,updated_at=? WHERE id=?', (file_id,(message.caption or '')[:1000],ts(),deposit['id']))
        await notify_deposit_admin(deposit['id'],user['id'],deposit['amount'],file_id,'document')
        return await message.answer('✅ رسید شارژ کیف پول دریافت شد.')
    await message.answer('📎 در حال حاضر فایلی برای ثبت وجود ندارد.')

# -------------------------------- account ------------------------------------

@router.callback_query(F.data == 'orders')
async def orders(call: CallbackQuery):
    await call.answer(); uid = (await ensure_user(call.from_user))['id']
    data = await rows('''SELECT o.*,COALESCE((SELECT GROUP_CONCAT(p.name,'، ') FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=o.id),'') names
                        FROM orders o WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 30''', (uid,))
    if not data: return await edit(call,'📭 سفارشی ندارید.',back())
    text='<b>📦 سفارش‌های من</b>\n\n'; buttons=[]
    for order in data:
        text += f'<code>{order["order_number"]}</code> — {esc(order["names"])}\n{money(order["payable_amount"])} {CURRENCY} — {status_text(order["status"])}\n\n'
        buttons.append([(f'🧾 {order["order_number"]}',f'order_view:{order["id"]}')])
    buttons.append([('🔙 خانه','home')]); await edit(call,text,kb(buttons))


@router.callback_query(F.data.startswith('order_view:'))
async def order_view(call: CallbackQuery):
    await call.answer(); uid=(await ensure_user(call.from_user))['id']; oid=int(call.data.split(':',1)[1])
    order=await one('SELECT * FROM orders WHERE id=? AND user_id=?',(oid,uid))
    if not order: return await call.answer('سفارش پیدا نشد.',show_alert=True)
    items=await rows('SELECT oi.*,p.name FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?',(oid,))
    text=(f'<b>🧾 {esc(order["order_number"])}</b>\n\nوضعیت: {status_text(order["status"])}\n'
          f'جمع: {money(order["subtotal"])} {CURRENCY}\nتخفیف: {money(order["discount_amount"])} {CURRENCY}\n'
          f'نهایی: <b>{money(order["payable_amount"])} {CURRENCY}</b>\n\n')
    for item in items: text += f'• {esc(item["name"])} × {item["quantity"]}\n'
    buttons=[]
    if order['status']=='PENDING_PAYMENT': buttons += [[('💳 پرداخت',f'pay_for:{oid}')],[('❌ لغو',f'cancel_order:{oid}')]]
    buttons.append([('🔙 سفارش‌ها','orders')]); await edit(call,text,kb(buttons))


@router.callback_query(F.data.startswith('pay_for:'))
async def pay_for(call: CallbackQuery):
    await call.answer(); oid=int(call.data.split(':',1)[1]); uid=(await ensure_user(call.from_user))['id']
    order=await one('SELECT * FROM orders WHERE id=? AND user_id=? AND status="PENDING_PAYMENT"',(oid,uid))
    if not order: return await call.answer('قابل پرداخت نیست.',show_alert=True)
    await edit(call,await pay_text(order),await order_payment_kb(oid))


@router.callback_query(F.data == 'account')
async def account(call: CallbackQuery):
    await call.answer(); user=await ensure_user(call.from_user)
    balance=await scalar('SELECT balance FROM wallets WHERE user_id=?',(user['id'],)) or 0
    total=await scalar('SELECT COUNT(*) FROM orders WHERE user_id=?',(user['id'],)) or 0
    completed=await scalar('SELECT COUNT(*) FROM orders WHERE user_id=? AND status="COMPLETED"',(user['id'],)) or 0
    await edit(call,
        f'<b>👤 حساب کاربری</b>\n\n🆔 <code>{user["telegram_id"]}</code>\n👤 @{esc(user["username"] or "-")}\n'
        f'📦 سفارش‌ها: {total}\n✅ تکمیل‌شده: {completed}\n💰 کیف پول: <b>{money(balance)} {CURRENCY}</b>',
        kb([[('📦 سفارش‌ها','orders'),('💰 کیف پول','wallet')],[('🎁 دعوت','referral')],[('🔙 خانه','home')]]))


@router.callback_query(F.data == 'wallet')
async def wallet(call: CallbackQuery):
    await call.answer(); user=await ensure_user(call.from_user); balance=await scalar('SELECT balance FROM wallets WHERE user_id=?',(user['id'],)) or 0
    txs=await rows('SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 8',(user['id'],))
    text=f'<b>💰 کیف پول</b>\n\nموجودی: <b>{money(balance)} {esc(CURRENCY)}</b>\n\n'
    for t in txs:
        sign='+' if t['type'] in ('DEPOSIT','REFUND','REFERRAL_REWARD') else '-'
        text+=f'{sign}{money(t["amount"])} — {esc(t["description"])}\n'
    await edit(call,text,kb([[('➕ افزایش موجودی','wallet_add')],[('📜 تراکنش‌ها','wallet_tx')],[('🔙 خانه','home')]]))


@router.callback_query(F.data == 'wallet_add')
async def wallet_add(call: CallbackQuery,state:FSMContext):
    await call.answer(); await state.set_state(WalletState.amount); await call.message.answer('💰 مبلغ شارژ را وارد کنید:')


@router.message(WalletState.amount)
async def wallet_amount(message: Message,state:FSMContext):
    try: amount=parse_amount(message.text or '')
    except ValueError: return await message.answer('❌ مبلغ نامعتبر.')
    await state.clear(); user=await ensure_user(message.from_user)
    cur=await execute('INSERT INTO wallet_deposits(user_id,amount,status,created_at,updated_at) VALUES(?,?,?,?,?)',(user['id'],amount,'PENDING',ts(),ts()))
    dep_id=cur.lastrowid
    await message.answer(f'💳 مبلغ: <b>{money(amount)} {CURRENCY}</b>\nکارت: <code>{esc(CARD_NUMBER)}</code>\nبه نام: <b>{esc(CARD_OWNER)}</b>\n\nرسید را همین‌جا بفرستید.')
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id,f'💰 درخواست شارژ #{dep_id}\nکاربر: <code>{user["telegram_id"]}</code>\nمبلغ: <b>{money(amount)} {CURRENCY}</b>\nبا ارسال رسید برای بررسی خواهد آمد.')
        except Exception: pass


@router.callback_query(F.data == 'wallet_tx')
async def wallet_tx(call: CallbackQuery):
    await call.answer(); uid=(await ensure_user(call.from_user))['id']; txs=await rows('SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(uid,))
    text='<b>📜 تراکنش‌های کیف پول</b>\n\n'
    for t in txs:
        sign='+' if t['type'] in ('DEPOSIT','REFUND','REFERRAL_REWARD') else '-'
        text+=f'{sign}{money(t["amount"])} {CURRENCY} — {esc(t["description"])}\n'
    await edit(call,text or '📭 تراکنشی ثبت نشده است.',back('wallet'))


async def register_referral(telegram_id:int, code:str):
    if not code.startswith('U'): return
    try: ref_user_id=int(code[1:])
    except ValueError: return
    target=await one('SELECT id FROM users WHERE telegram_id=?',(telegram_id,))
    referrer=await one('SELECT id FROM users WHERE id=?',(ref_user_id,))
    if not target or not referrer or target['id']==referrer['id']: return
    await execute('''INSERT OR IGNORE INTO referrals(referrer_user_id,referred_user_id,code,reward_amount,created_at)
                     VALUES(?,?,?,?,?)''',(referrer['id'],target['id'],token('REF-'),REFERRAL_REWARD,ts()))


async def complete_referral(user_id:Optional[int]):
    if not user_id or REFERRAL_REWARD<=0: return
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            ref=await (await db.execute('SELECT * FROM referrals WHERE referred_user_id=? AND status="PENDING"',(user_id,))).fetchone()
            if not ref: await db.rollback(); return
            wallet=await (await db.execute('SELECT * FROM wallets WHERE user_id=?',(ref['referrer_user_id'],))).fetchone()
            if not wallet: await db.rollback(); return
            before=wallet['balance']; after=before+REFERRAL_REWARD; stamp=ts()
            await db.execute('UPDATE wallets SET balance=?,updated_at=? WHERE user_id=?',(after,stamp,ref['referrer_user_id']))
            await db.execute('''INSERT INTO wallet_transactions(user_id,type,amount,balance_before,balance_after,reference_type,reference_id,description,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)''',(ref['referrer_user_id'],'REFERRAL_REWARD',REFERRAL_REWARD,'{}'.format(before),'{}'.format(after),'REFERRAL',str(ref['id']),'پاداش دعوت',stamp))
            await db.execute('UPDATE referrals SET status="COMPLETED",reward_amount=?,completed_at=? WHERE id=?',(REFERRAL_REWARD,stamp,ref['id']))
            referred=await (await db.execute('SELECT telegram_id FROM users WHERE id=?',(ref['referrer_user_id'],))).fetchone()
            await db.commit()
    try: await bot.send_message(referred['telegram_id'],f'🎁 پاداش دعوت شما: <b>{money(REFERRAL_REWARD)} {CURRENCY}</b>')
    except Exception: pass


@router.callback_query(F.data == 'referral')
async def referral(call: CallbackQuery):
    await call.answer(); user=await ensure_user(call.from_user); me=await bot.me()
    total=await scalar('SELECT COUNT(*) FROM referrals WHERE referrer_user_id=?',(user['id'],)) or 0
    done=await scalar('SELECT COUNT(*) FROM referrals WHERE referrer_user_id=? AND status="COMPLETED"',(user['id'],)) or 0
    reward=await scalar('SELECT COALESCE(SUM(reward_amount),0) FROM referrals WHERE referrer_user_id=? AND status="COMPLETED"',(user['id'],)) or 0
    link=f'https://t.me/{me.username}?start=ref_U{user["id"]}'
    await edit(call,
        f'<b>🎁 دعوت دوستان</b>\n\nدعوت‌ها: {total}\nموفق: {done}\nپاداش: {money(reward)} {CURRENCY}\n\n'
        f'🔗 <code>{esc(link)}</code>\n🎁 پاداش هر دعوت موفق: {money(REFERRAL_REWARD)} {CURRENCY}',back())


@router.callback_query(F.data == 'discount')
async def discount_page(call: CallbackQuery):
    await call.answer(); await edit(call,'<b>🎟 کد تخفیف</b>\n\nکدهای تخفیف در مرحله پرداخت قابل اعمال هستند.',back())


@router.callback_query(F.data == 'guide')
async def guide(call: CallbackQuery):
    await call.answer(); await edit(call,
        '<b>📚 راهنما</b>\n\n1️⃣ محصول را انتخاب کنید.\n2️⃣ به سبد اضافه کنید.\n3️⃣ سفارش بسازید.\n'
        '4️⃣ در صورت نیاز کد تخفیف بزنید.\n5️⃣ پرداخت کنید.\n6️⃣ پس از تأیید، کانفیگ تحویل می‌شود.\n\n'
        '🔒 موجودی در لحظه ساخت سفارش رزرو می‌شود و در انقضا/لغو آزاد می‌گردد.',back())

# -------------------------------- support tickets -----------------------------

@router.callback_query(F.data == 'support')
async def support(call: CallbackQuery):
    await call.answer(); await edit(call,'<b>🆘 پشتیبانی</b>\n\nموضوع را انتخاب کنید:',kb([
        [('💳 پرداخت','ticket:PAYMENT'),('🛍 خرید','ticket:ORDER')],
        [('🔐 کانفیگ','ticket:CONFIG'),('👤 حساب','ticket:ACCOUNT')],
        [('📝 سایر','ticket:OTHER')],[('📂 تیکت‌های من','my_tickets')],[('🔙 خانه','home')]
    ]))


@router.callback_query(F.data.startswith('ticket:'))
async def ticket_start(call: CallbackQuery,state:FSMContext):
    await call.answer(); await state.update_data(category=call.data.split(':',1)[1]); await state.set_state(TicketState.subject); await call.message.answer('📝 موضوع تیکت را بنویسید:')


@router.message(TicketState.subject)
async def ticket_subject(message: Message,state:FSMContext):
    subject=(message.text or '').strip()[:255]
    if len(subject)<3: return await message.answer('حداقل ۳ کاراکتر وارد کنید.')
    await state.update_data(subject=subject); await state.set_state(TicketState.body); await message.answer('💬 متن پیام را بنویسید:')


@router.message(TicketState.body)
async def ticket_body(message: Message,state:FSMContext):
    data=await state.get_data(); await state.clear(); user=await ensure_user(message.from_user); number=token('T-')
    body=(message.text or '').strip()[:4000]
    if not body: return await message.answer('❌ متن تیکت خالی است.')
    async with DB_LOCK:
        async with conn() as db:
            cur=await db.execute('''INSERT INTO tickets(ticket_number,user_id,category,subject,status,created_at,updated_at)
                                    VALUES(?,?,?,?,?,?,?)''',(number,user['id'],data['category'],data['subject'],'OPEN',ts(),ts()))
            tid=cur.lastrowid
            await db.execute('''INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,message,telegram_message_id,created_at)
                                VALUES(?,?,?,?,?,?)''',(tid,'USER',user['id'],body,message.message_id,ts()))
            await db.commit()
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id,f'🆘 <b>تیکت {number}</b>\nموضوع: {esc(data["subject"])}\nکاربر: <code>{user["telegram_id"]}</code>\n\n{esc(body)}\n/reply {tid}')
        except Exception: pass
    await message.answer(f'✅ تیکت <code>{number}</code> ثبت شد.',reply_markup=home_kb(is_admin(message.from_user.id)))


@router.callback_query(F.data == 'my_tickets')
async def my_tickets(call: CallbackQuery):
    await call.answer(); uid=(await ensure_user(call.from_user))['id']; tickets=await rows('SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC LIMIT 30',(uid,))
    buttons=[[(f'{status_text(t["status"])} {t["ticket_number"]} | {t["subject"]}',f'ticket_view:{t["id"]}')] for t in tickets]
    buttons.append([('🔙 پشتیبانی','support')]); await edit(call,'<b>📂 تیکت‌های من</b>',kb(buttons))


@router.callback_query(F.data.startswith('ticket_view:'))
async def ticket_view(call: CallbackQuery):
    await call.answer(); uid=(await ensure_user(call.from_user))['id']; tid=int(call.data.split(':',1)[1])
    ticket=await one('SELECT * FROM tickets WHERE id=? AND user_id=?',(tid,uid))
    if not ticket: return await call.answer('تیکت پیدا نشد.',show_alert=True)
    messages=await rows('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at DESC LIMIT 20',(tid,))
    text=f'<b>🆘 {esc(ticket["ticket_number"])}</b>\nموضوع: {esc(ticket["subject"])}\nوضعیت: {status_text(ticket["status"])}\n\n'
    for item in reversed(messages): text += f'<b>{"👤 شما" if item["sender_type"]=="USER" else "🛠 پشتیبانی"}</b>\n{esc(item["message"])}\n\n'
    buttons=[]
    if ticket['status']!='CLOSED': buttons.append([('💬 پاسخ',f'ticket_reply:{tid}'),('🔒 بستن',f'ticket_close:{tid}')])
    buttons.append([('🔙 تیکت‌ها','my_tickets')]); await edit(call,text,kb(buttons))


@router.callback_query(F.data.startswith('ticket_reply:'))
async def ticket_reply_start(call: CallbackQuery,state:FSMContext):
    await call.answer(); tid=int(call.data.split(':',1)[1]); uid=(await ensure_user(call.from_user))['id']
    if not await scalar('SELECT 1 FROM tickets WHERE id=? AND user_id=? AND status<>"CLOSED"',(tid,uid)):
        return await call.answer('تیکت بسته است.',show_alert=True)
    await state.update_data(ticket_id=tid); await state.set_state(TicketReplyState.body); await call.message.answer('💬 پاسخ خود را بفرستید:')


@router.message(TicketReplyState.body)
async def ticket_reply_user(message: Message,state:FSMContext):
    data=await state.get_data(); await state.clear(); tid=int(data['ticket_id']); user=await ensure_user(message.from_user); body=(message.text or '').strip()[:4000]
    if not body: return await message.answer('❌ پیام خالی است.')
    ticket=await one('SELECT * FROM tickets WHERE id=? AND user_id=? AND status<>"CLOSED"',(tid,user['id']))
    if not ticket: return await message.answer('❌ تیکت معتبر نیست.')
    await execute('''INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,message,telegram_message_id,created_at)
                     VALUES(?,?,?,?,?,?)''',(tid,'USER',user['id'],body,message.message_id,ts()))
    await execute('UPDATE tickets SET status="OPEN",updated_at=? WHERE id=?',(ts(),tid))
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id,f'💬 پاسخ تیکت #{tid} از <code>{user["telegram_id"]}</code>:\n{esc(body)}\n/reply {tid}')
        except Exception: pass
    await message.answer('✅ پاسخ ثبت شد.')


@router.callback_query(F.data.startswith('ticket_close:'))
async def ticket_close(call: CallbackQuery):
    await call.answer(); tid=int(call.data.split(':',1)[1]); uid=(await ensure_user(call.from_user))['id']
    await execute('UPDATE tickets SET status="CLOSED",closed_at=?,updated_at=? WHERE id=? AND user_id=?',(ts(),ts(),tid,uid))
    await edit(call,'🔒 تیکت بسته شد.',back('my_tickets'))

# ------------------------------- admin common ----------------------------------

async def admin_home(target: Message|CallbackQuery):
    stats=await one('''SELECT
      (SELECT COUNT(*) FROM users) users,
      (SELECT COUNT(*) FROM users WHERE last_seen_at>=?) active,
      (SELECT COUNT(*) FROM products WHERE is_active=1) products,
      (SELECT COUNT(*) FROM configs WHERE status='AVAILABLE') stock,
      (SELECT COUNT(*) FROM configs WHERE status='RESERVED') reserved,
      (SELECT COUNT(*) FROM orders) orders,
      (SELECT COUNT(*) FROM orders WHERE status='COMPLETED') completed,
      (SELECT COALESCE(SUM(payable_amount),0) FROM orders WHERE status='COMPLETED') revenue,
      (SELECT COUNT(*) FROM payments WHERE status='PENDING') pending_payments,
      (SELECT COUNT(*) FROM tickets WHERE status<>'CLOSED') open_tickets''',
      ((now()-timedelta(days=1)).isoformat(timespec='seconds'),))
    text=(f'<b>⚙️ {esc(SHOP_NAME)} — مدیریت</b>\n\n👥 کاربران: {stats["users"]}\n🟢 فعال ۲۴ساعت: {stats["active"]}\n'
          f'🛍 محصولات: {stats["products"]}\n📦 موجود: {stats["stock"]}\n🔒 رزرو: {stats["reserved"]}\n'
          f'🧾 سفارش‌ها: {stats["orders"]}\n🎉 فروش موفق: {stats["completed"]}\n💰 درآمد: {money(stats["revenue"])} {CURRENCY}\n'
          f'💳 پرداخت در انتظار: {stats["pending_payments"]}\n🆘 تیکت باز: {stats["open_tickets"]}')
    if isinstance(target,CallbackQuery): await edit(target,text,admin_kb())
    else: await target.answer(text,reply_markup=admin_kb())


@router.callback_query(F.data=='admin')
async def admin(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await admin_home(call)


@router.callback_query(F.data=='a_dashboard')
async def a_dashboard(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await admin_home(call)


@router.callback_query(F.data=='a_inventory')
async def a_inventory(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer()
    data=await rows('''SELECT p.name,
      SUM(CASE WHEN c.status='AVAILABLE' THEN 1 ELSE 0 END) available,
      SUM(CASE WHEN c.status='RESERVED' THEN 1 ELSE 0 END) reserved,
      SUM(CASE WHEN c.status='SOLD' THEN 1 ELSE 0 END) sold,
      SUM(CASE WHEN c.status='DISABLED' THEN 1 ELSE 0 END) disabled
      FROM products p LEFT JOIN configs c ON c.product_id=p.id GROUP BY p.id ORDER BY p.id DESC''')
    text='<b>🔐 موجودی</b>\n\n'
    for item in data: text += f'• {esc(item["name"])}\n🟢 {item["available"]} | 🔒 {item["reserved"]} | 🔴 {item["sold"]} | ⚪ {item["disabled"]}\n\n'
    await edit(call,text or '📭 موجودی ثبت نشده است.',back('admin'))

# -------------------------------- admin products ------------------------------

@router.callback_query(F.data=='a_products')
async def a_products(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer()
    products=await rows('''SELECT p.*,(SELECT COUNT(*) FROM configs c WHERE c.product_id=p.id AND c.status='AVAILABLE') stock
                           FROM products p ORDER BY p.id DESC LIMIT 60''')
    buttons=[[('➕ افزودن محصول','a_add_product')]]
    buttons += [[(f'{"🟢" if p["is_active"] else "⚪"} {p["name"]} | {money(p["price"])}',f'a_product:{p["id"]}')] for p in products]
    buttons.append([('🔙 پنل','admin')]); await edit(call,'<b>🛍 مدیریت محصولات</b>',kb(buttons))


@router.callback_query(F.data=='a_add_product')
async def a_add_product(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await state.set_state(ProductState.name); await call.message.answer('نام محصول:')


@router.message(ProductState.name)
async def p_name(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    value=(message.text or '').strip()[:200]
    if not value: return await message.answer('❌ نام خالی است.')
    await state.update_data(name=value); await state.set_state(ProductState.category)
    cats=await rows('SELECT id,name FROM categories WHERE is_active=1 ORDER BY sort_order,id')
    await message.answer('دسته‌بندی:',reply_markup=kb([[(c['name'],f'pickcat:{c["id"]}')] for c in cats]))


@router.callback_query(F.data.startswith('pickcat:'))
async def p_category(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await state.update_data(category_id=int(call.data.split(':',1)[1])); await state.set_state(ProductState.protocol); await call.message.answer('پروتکل (مثل VLESS):')


@router.message(ProductState.protocol)
async def p_protocol(message: Message,state:FSMContext):
    value=(message.text or '').strip()[:50].upper()
    if not value: return await message.answer('❌ پروتکل خالی است.')
    await state.update_data(protocol=value); await state.set_state(ProductState.price); await message.answer('قیمت به تومان:')


@router.message(ProductState.price)
async def p_price(message: Message,state:FSMContext):
    try: value=int((message.text or '').replace(',','').replace('٬','').strip()); assert value>=0
    except Exception: return await message.answer('❌ قیمت نامعتبر.')
    await state.update_data(price=value); await state.set_state(ProductState.duration); await message.answer('اعتبار به روز:')


@router.message(ProductState.duration)
async def p_duration(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 1<=value<=3650
    except Exception: return await message.answer('❌ عدد نامعتبر.')
    await state.update_data(duration=value); await state.set_state(ProductState.traffic); await message.answer('حجم به GB؛ صفر = نامحدود:')


@router.message(ProductState.traffic)
async def p_traffic(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=1000000
    except Exception: return await message.answer('❌ عدد نامعتبر.')
    await state.update_data(traffic=None if value==0 else value); await state.set_state(ProductState.devices); await message.answer('تعداد دستگاه؛ صفر = نامحدود:')


@router.message(ProductState.devices)
async def p_devices(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=100
    except Exception: return await message.answer('❌ عدد نامعتبر.')
    await state.update_data(devices=None if value==0 else value); await state.set_state(ProductState.description); await message.answer('توضیحات؛ برای خالی بودن - بفرستید:')


@router.message(ProductState.description)
async def p_description(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    data=await state.get_data(); await state.clear(); description='' if (message.text or '').strip()=='-' else (message.text or '').strip()[:2000]
    slug=re.sub(r'[^a-zA-Z0-9_-]+','-',data['name'].lower()).strip('-')+'-'+secrets.token_hex(3)
    cur=await execute('''INSERT INTO products(category_id,name,slug,description,protocol,price,currency,duration_days,traffic_gb,device_limit,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (data['category_id'],data['name'],slug,description,data['protocol'],data['price'],CURRENCY,data['duration'],data['traffic'],data['devices'],ts(),ts()))
    await audit(message.from_user.id,'CREATE_PRODUCT','PRODUCT',cur.lastrowid,data['name']); await message.answer('✅ محصول ساخته شد.',reply_markup=home_kb(True))


@router.callback_query(F.data.startswith('a_product:'))
async def a_product(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); pid=int(call.data.split(':',1)[1])
    p=await one('''SELECT p.*,
      (SELECT COUNT(*) FROM configs c WHERE c.product_id=p.id AND c.status='AVAILABLE') stock,
      (SELECT COUNT(*) FROM configs c WHERE c.product_id=p.id AND c.status='RESERVED') reserved,
      (SELECT COUNT(*) FROM configs c WHERE c.product_id=p.id AND c.status='SOLD') sold
      FROM products p WHERE p.id=?''',(pid,))
    if not p: return await call.answer('پیدا نشد.',show_alert=True)
    text=(f'<b>🛍 {esc(p["name"])}</b>\n\nقیمت: {money(p["price"])} {CURRENCY}\nپروتکل: {esc(p["protocol"])}\n'
          f'اعتبار: {p["duration_days"]} روز\nموجود: {p["stock"]}\nرزرو: {p["reserved"]}\nفروخته: {p["sold"]}\n'
          f'وضعیت: {"فعال" if p["is_active"] else "خاموش"}\n\n{esc(p["description"] or "-")}')
    await edit(call,text,kb([[('➕ افزودن کانفیگ',f'a_add_config:{pid}'),('📥 ورود گروهی',f'a_bulk_config:{pid}')],
                              [('🔄 فعال/غیرفعال',f'a_toggle_product:{pid}')],[('🗑 حذف',f'a_delete_product:{pid}')],[('🔙 محصولات','a_products')]]))


@router.callback_query(F.data.startswith('a_toggle_product:'))
async def a_toggle_product(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    pid=int(call.data.split(':',1)[1]); product=await one('SELECT is_active FROM products WHERE id=?',(pid,))
    if not product: return await call.answer('پیدا نشد.',show_alert=True)
    await execute('UPDATE products SET is_active=?,updated_at=? WHERE id=?',(0 if product['is_active'] else 1,ts(),pid))
    await audit(call.from_user.id,'TOGGLE_PRODUCT','PRODUCT',pid); await call.answer('✅ انجام شد'); await a_product(call)


@router.callback_query(F.data.startswith('a_delete_product:'))
async def a_delete_product(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    pid=int(call.data.split(':',1)[1]); sold=int(await scalar('SELECT COUNT(*) FROM configs WHERE product_id=? AND status="SOLD"',(pid,)) or 0)
    if sold: return await call.answer('محصول دارای فروش است؛ فقط غیرفعال کنید.',show_alert=True)
    await execute('DELETE FROM products WHERE id=?',(pid,)); await audit(call.from_user.id,'DELETE_PRODUCT','PRODUCT',pid); await call.answer('🗑 حذف شد'); await a_products(call)


@router.callback_query(F.data.startswith('a_add_config:'))
async def a_add_config(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    pid=int(call.data.split(':',1)[1]); await state.update_data(product_id=pid); await state.set_state(ConfigState.payload); await call.answer(); await call.message.answer('🔐 کانفیگ‌ها را در چند خط ارسال کنید؛ هر کانفیگ در یک خط.')


@router.callback_query(F.data.startswith('a_bulk_config:'))
async def a_bulk_config(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    pid=int(call.data.split(':',1)[1]); await state.update_data(product_id=pid); await state.set_state(ConfigState.payload); await call.answer(); await call.message.answer('📥 حداکثر 500 کانفیگ در هر پیام؛ هر مورد در یک خط.')


@router.message(ConfigState.payload)
async def save_configs(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    data=await state.get_data(); await state.clear(); pid=int(data['product_id']); raw=(message.text or message.caption or '').strip()
    values=[line.strip() for line in raw.splitlines() if line.strip()][:500]
    if not values: return await message.answer('❌ ورودی خالی.')
    async with DB_LOCK:
        async with conn() as db:
            for value in values:
                await db.execute('INSERT INTO configs(product_id,config_text,status,created_at) VALUES(?,?,?,?)',(pid,value,'AVAILABLE',ts()))
            await db.commit()
    await audit(message.from_user.id,'ADD_CONFIGS','PRODUCT',pid,f'count={len(values)}'); await message.answer(f'✅ {len(values)} کانفیگ ثبت شد.')

# ----------------------------- admin orders/payments --------------------------

@router.callback_query(F.data == 'a_orders')
async def a_orders(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer()
    data=await rows('''SELECT o.id,o.order_number,o.status,o.payable_amount,u.telegram_id
                       FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created_at DESC LIMIT 60''')
    buttons=[[(f'{status_text(x["status"])} {x["order_number"]} | {money(x["payable_amount"])}',f'a_order:{x["id"]}')] for x in data]
    buttons.append([('🔙 پنل','admin')]); await edit(call,'<b>🧾 سفارش‌ها</b>\n\nجدیدترین سفارش‌ها:',kb(buttons))


@router.callback_query(F.data.startswith('a_order:'))
async def a_order(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); oid=int(call.data.split(':',1)[1])
    order=await one('SELECT o.*,u.telegram_id,u.username FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?',(oid,))
    if not order: return await call.answer('پیدا نشد.',show_alert=True)
    items=await rows('SELECT oi.*,p.name FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?',(oid,))
    text=(f'<b>🧾 {esc(order["order_number"])}</b>\n\nکاربر: <code>{order["telegram_id"]}</code> @{esc(order["username"] or "-")}\n'
          f'وضعیت: {status_text(order["status"])}\nمبلغ: {money(order["payable_amount"])} {CURRENCY}\n\n')
    for item in items: text+=f'• {esc(item["name"])} × {item["quantity"]}\n'
    buttons=[]
    if order['status'] in ('PENDING_PAYMENT','AWAITING_RECEIPT'): buttons.append([('🚫 لغو',f'a_cancel_order:{oid}')])
    buttons.append([('🔙 سفارش‌ها','a_orders')]); await edit(call,text,kb(buttons))


@router.callback_query(F.data.startswith('a_cancel_order:'))
async def a_cancel_order(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    oid=int(call.data.split(':',1)[1]); ok=await cancel_order_db(oid)
    if ok: await audit(call.from_user.id,'ADMIN_CANCEL_ORDER','ORDER',oid)
    await call.answer('✅ لغو شد' if ok else 'قابل لغو نیست.',show_alert=not ok); await a_orders(call)


@router.callback_query(F.data == 'a_payments')
async def a_payments(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer()
    data=await rows('''SELECT p.id,p.method,p.amount,p.status,o.order_number,u.telegram_id
                       FROM payments p JOIN orders o ON o.id=p.order_id JOIN users u ON u.id=p.user_id
                       ORDER BY p.created_at DESC LIMIT 60''')
    text='<b>💳 پرداخت‌ها</b>\n\n'
    for p in data: text+=f'#{p["id"]} | {esc(p["order_number"])} | {esc(p["method"])} | {money(p["amount"])} | {esc(p["status"])}\n'
    await edit(call,text or '📭 پرداختی نیست.',back('admin'))


@router.callback_query(F.data.startswith('approve_payment:'))
async def approve_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); pid=int(call.data.split(':',1)[1])
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            payment=await (await db.execute('SELECT * FROM payments WHERE id=?',(pid,))).fetchone()
            if not payment or payment['status']!='PENDING':
                await db.rollback(); return await call.answer('قبلاً بررسی شده.',show_alert=True)
            order=await (await db.execute('SELECT * FROM orders WHERE id=?',(payment['order_id'],))).fetchone()
            if not order or order['status']!='PAYMENT_REVIEW':
                await db.rollback(); return await call.answer('سفارش در انتظار بررسی نیست.',show_alert=True)
            stamp=ts()
            await db.execute('UPDATE payments SET status="VERIFIED",reviewed_at=?,reviewed_by=?,updated_at=? WHERE id=?',(stamp,call.from_user.id,stamp,pid))
            await db.execute('UPDATE orders SET status="PAID",payment_status="PAID",updated_at=? WHERE id=?',(stamp,order['id']))
            await db.commit()
    configs=await claim_reserved_configs(payment['order_id'])
    if not configs:
        await execute('UPDATE payments SET status="NEEDS_REFUND",updated_at=? WHERE id=?',(ts(),pid))
        await execute('UPDATE orders SET status="REJECTED",payment_status="NEEDS_REFUND",updated_at=? WHERE id=?',(ts(),payment['order_id']))
        await release_order_reservations(payment['order_id'])
        await call.answer('⛔ پرداخت تأیید شد اما رزرو معتبر نیست؛ نیاز به بازگشت وجه.',show_alert=True)
        return
    await notify_delivery(payment['order_id'],configs)
    await audit(call.from_user.id,'APPROVE_PAYMENT','PAYMENT',pid,order['order_number'])
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await call.answer('✅ تأیید و تحویل شد.')


@router.callback_query(F.data.startswith('reject_payment:'))
async def reject_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); pid=int(call.data.split(':',1)[1]); payment=await one('SELECT * FROM payments WHERE id=?',(pid,))
    if not payment or payment['status']!='PENDING': return await call.answer('قبلاً بررسی شده.',show_alert=True)
    await execute('UPDATE payments SET status="REJECTED",reviewed_at=?,reviewed_by=?,updated_at=? WHERE id=?',(ts(),call.from_user.id,ts(),pid))
    await cancel_order_db(payment['order_id'])
    user=await one('SELECT telegram_id FROM users WHERE id=?',(payment['user_id'],))
    if user:
        try: await bot.send_message(user['telegram_id'],'❌ رسید پرداخت سفارش شما رد شد. مبلغی از حساب کیف پول شما کم نشده است. برای پیگیری تیکت بزنید.')
        except Exception: pass
    await audit(call.from_user.id,'REJECT_PAYMENT','PAYMENT',pid)
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await call.answer('❌ رد شد.')

# -------------------------------- admin wallet --------------------------------

@router.callback_query(F.data == 'a_wallet')
async def a_wallet(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); total=int(await scalar('SELECT COALESCE(SUM(balance),0) FROM wallets') or 0)
    deposits=await rows('''SELECT d.id,d.amount,u.telegram_id FROM wallet_deposits d JOIN users u ON u.id=d.user_id
                           WHERE d.status='PENDING' ORDER BY d.created_at DESC LIMIT 40''')
    text=f'<b>💰 کیف پول</b>\n\nمجموع موجودی کاربران: <b>{money(total)} {CURRENCY}</b>\n\nدرخواست‌های در انتظار:\n'
    buttons=[[(f'💳 شارژ #{d["id"]} | {money(d["amount"])} | {d["telegram_id"]}',f'a_deposit:{d["id"]}')] for d in deposits]
    buttons.append([('🔙 پنل','admin')]); await edit(call,text,kb(buttons))


@router.callback_query(F.data.startswith('a_deposit:'))
async def a_deposit(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); did=int(call.data.split(':',1)[1]); deposit=await one('SELECT d.*,u.telegram_id,u.username FROM wallet_deposits d JOIN users u ON u.id=d.user_id WHERE d.id=?',(did,))
    if not deposit: return await call.answer('پیدا نشد.',show_alert=True)
    text=f'<b>💰 درخواست شارژ #{did}</b>\n\nکاربر: <code>{deposit["telegram_id"]}</code> @{esc(deposit["username"] or "-")}\nمبلغ: <b>{money(deposit["amount"])} {CURRENCY}</b>\nوضعیت: {deposit["status"]}'
    await edit(call,text,kb([[('✅ تأیید',f'approve_deposit:{did}'),('❌ رد',f'reject_deposit:{did}')],[('🔙 کیف پول','a_wallet')]]))


@router.callback_query(F.data.startswith('approve_deposit:'))
async def approve_deposit(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); did=int(call.data.split(':',1)[1])
    async with DB_LOCK:
        async with conn() as db:
            await db.execute('BEGIN IMMEDIATE')
            deposit=await (await db.execute('SELECT * FROM wallet_deposits WHERE id=?',(did,))).fetchone()
            if not deposit or deposit['status']!='PENDING':
                await db.rollback(); return await call.answer('قبلاً بررسی شده.',show_alert=True)
            wallet=await (await db.execute('SELECT * FROM wallets WHERE user_id=?',(deposit['user_id'],))).fetchone()
            before=int(wallet['balance']); after=before+int(deposit['amount']); stamp=ts()
            await db.execute('UPDATE wallets SET balance=?,updated_at=? WHERE user_id=?',(after,stamp,deposit['user_id']))
            await db.execute('''INSERT INTO wallet_transactions(user_id,type,amount,balance_before,balance_after,reference_type,reference_id,description,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?)''',(deposit['user_id'],'DEPOSIT',deposit['amount'],before,after,'DEPOSIT',str(did),'افزایش موجودی',stamp))
            await db.execute('UPDATE wallet_deposits SET status="APPROVED",reviewed_by=?,reviewed_at=?,updated_at=? WHERE id=?',(call.from_user.id,stamp,stamp,did))
            user=await (await db.execute('SELECT telegram_id FROM users WHERE id=?',(deposit['user_id'],))).fetchone()
            await db.commit()
    try: await bot.send_message(user['telegram_id'],f'✅ کیف پول شما {money(deposit["amount"])} {CURRENCY} شارژ شد.')
    except Exception: pass
    await audit(call.from_user.id,'APPROVE_DEPOSIT','DEPOSIT',did); await a_wallet(call)


@router.callback_query(F.data.startswith('reject_deposit:'))
async def reject_deposit(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    did=int(call.data.split(':',1)[1]); deposit=await one('SELECT * FROM wallet_deposits WHERE id=?',(did,))
    if not deposit or deposit['status']!='PENDING': return await call.answer('قبلاً بررسی شده.',show_alert=True)
    await execute('UPDATE wallet_deposits SET status="REJECTED",reviewed_by=?,reviewed_at=?,updated_at=? WHERE id=?',(call.from_user.id,ts(),ts(),did))
    user=await one('SELECT telegram_id FROM users WHERE id=?',(deposit['user_id'],))
    if user:
        try: await bot.send_message(user['telegram_id'],f'❌ درخواست شارژ #{did} رد شد.')
        except Exception: pass
    await audit(call.from_user.id,'REJECT_DEPOSIT','DEPOSIT',did); await call.answer('❌ رد شد.'); await a_wallet(call)

# -------------------------------- discounts ----------------------------------

@router.callback_query(F.data == 'a_discounts')
async def a_discounts(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await rows('SELECT * FROM discounts ORDER BY created_at DESC LIMIT 60')
    buttons=[[('➕ ساخت کد','a_add_discount')]]
    buttons += [[(f'{"🟢" if x["is_active"] else "⚪"} {x["code"]} | {x["type"]} {x["value"]}',f'a_discount:{x["id"]}')] for x in data]
    buttons.append([('🔙 پنل','admin')]); await edit(call,'<b>🎟 کدهای تخفیف</b>',kb(buttons))


@router.callback_query(F.data == 'a_add_discount')
async def add_discount(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await state.set_state(DiscountState.code); await call.message.answer('کد تخفیف (۳ تا ۵۰ کاراکتر A-Z/0-9/_/-):')


@router.message(DiscountState.code)
async def discount_code(message: Message,state:FSMContext):
    code=(message.text or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{3,50}',code): return await message.answer('❌ فرمت نامعتبر.')
    if await scalar('SELECT 1 FROM discounts WHERE code=?',(code,)): return await message.answer('❌ کد تکراری است.')
    await state.update_data(code=code); await state.set_state(DiscountState.typ); await message.answer('نوع را بفرستید: PERCENT یا FIXED')


@router.message(DiscountState.typ)
async def discount_type(message: Message,state:FSMContext):
    typ=(message.text or '').strip().upper()
    if typ not in ('PERCENT','FIXED'): return await message.answer('فقط PERCENT یا FIXED')
    await state.update_data(typ=typ); await state.set_state(DiscountState.value); await message.answer('مقدار:')


@router.message(DiscountState.value)
async def discount_value(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=10**12
    except Exception: return await message.answer('❌ مقدار نامعتبر.')
    data=await state.get_data()
    if data['typ']=='PERCENT' and value>100: return await message.answer('درصد باید حداکثر 100 باشد.')
    await state.update_data(value=value); await state.set_state(DiscountState.minimum); await message.answer('حداقل مبلغ سفارش؛ 0 = بدون حداقل:')


@router.message(DiscountState.minimum)
async def discount_min(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=10**12
    except Exception: return await message.answer('❌ نامعتبر.')
    await state.update_data(minimum=value); await state.set_state(DiscountState.maximum); await message.answer('سقف تخفیف؛ 0 = بدون سقف:')


@router.message(DiscountState.maximum)
async def discount_max(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=10**12
    except Exception: return await message.answer('❌ نامعتبر.')
    await state.update_data(maximum=None if value==0 else value); await state.set_state(DiscountState.limit); await message.answer('سقف مصرف؛ 0 = نامحدود:')


@router.message(DiscountState.limit)
async def discount_limit(message: Message,state:FSMContext):
    try: value=int((message.text or '').strip()); assert 0<=value<=10**9
    except Exception: return await message.answer('❌ نامعتبر.')
    await state.update_data(limit=None if value==0 else value); await state.set_state(DiscountState.expires); await message.answer('انقضا YYYY-MM-DD یا - :')


@router.message(DiscountState.expires)
async def discount_expires(message: Message,state:FSMContext):
    raw=(message.text or '').strip(); expires=None
    if raw!='-':
        try: expires=datetime.fromisoformat(raw+'T23:59:59+00:00').isoformat(timespec='seconds')
        except ValueError: return await message.answer('❌ تاریخ نامعتبر.')
    data=await state.get_data(); await state.clear()
    cur=await execute('''INSERT INTO discounts(code,type,value,minimum_order_amount,maximum_discount,usage_limit,expires_at,created_at)
                         VALUES(?,?,?,?,?,?,?,?)''',(data['code'],data['typ'],data['value'],data['minimum'],data['maximum'],data['limit'],expires,ts()))
    await audit(message.from_user.id,'CREATE_DISCOUNT','DISCOUNT',cur.lastrowid,data['code']); await message.answer(f'✅ کد <code>{esc(data["code"])}</code> ساخته شد.')


@router.callback_query(F.data.startswith('a_discount:'))
async def a_discount(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); did=int(call.data.split(':',1)[1]); d=await one('SELECT * FROM discounts WHERE id=?',(did,))
    if not d: return await call.answer('پیدا نشد.',show_alert=True)
    await edit(call,f'<b>🎟 {esc(d["code"])}</b>\n\nنوع: {d["type"]}\nمقدار: {d["value"]}\nمصرف: {d["usage_count"]}/{d["usage_limit"] or "∞"}\nفعال: {"بله" if d["is_active"] else "خیر"}',
               kb([[('🔄 تغییر وضعیت',f'a_discount_toggle:{did}'),('🗑 حذف',f'a_discount_delete:{did}')],[('🔙 تخفیف‌ها','a_discounts')]]))


@router.callback_query(F.data.startswith('a_discount_toggle:'))
async def discount_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    did=int(call.data.split(':',1)[1]); d=await one('SELECT is_active FROM discounts WHERE id=?',(did,))
    if not d: return await call.answer('پیدا نشد.',show_alert=True)
    await execute('UPDATE discounts SET is_active=? WHERE id=?',(0 if d['is_active'] else 1,did)); await audit(call.from_user.id,'TOGGLE_DISCOUNT','DISCOUNT',did); await call.answer('✅ انجام شد'); await a_discounts(call)


@router.callback_query(F.data.startswith('a_discount_delete:'))
async def discount_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    did=int(call.data.split(':',1)[1]); await execute('DELETE FROM discounts WHERE id=?',(did,)); await audit(call.from_user.id,'DELETE_DISCOUNT','DISCOUNT',did); await call.answer('🗑 حذف شد'); await a_discounts(call)

# -------------------------------- admin tickets -------------------------------

@router.callback_query(F.data == 'a_tickets')
async def a_tickets(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await rows('''SELECT t.*,u.telegram_id FROM tickets t JOIN users u ON u.id=t.user_id
                                             WHERE t.status<>"CLOSED" ORDER BY t.updated_at DESC LIMIT 60''')
    buttons=[[(f'{status_text(t["status"])} {t["ticket_number"]} | {t["telegram_id"]}',f'a_ticket:{t["id"]}')] for t in data]
    buttons.append([('🔙 پنل','admin')]); await edit(call,'<b>🆘 تیکت‌های باز</b>',kb(buttons))


@router.callback_query(F.data.startswith('a_ticket:'))
async def a_ticket(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); tid=int(call.data.split(':',1)[1]); ticket=await one('SELECT t.*,u.telegram_id,u.username FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?',(tid,))
    if not ticket: return await call.answer('پیدا نشد.',show_alert=True)
    messages=await rows('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at DESC LIMIT 20',(tid,))
    text=f'<b>🆘 {esc(ticket["ticket_number"])}</b>\nکاربر: <code>{ticket["telegram_id"]}</code>\nموضوع: {esc(ticket["subject"])}\nوضعیت: {status_text(ticket["status"])}\n\n'
    for item in reversed(messages): text += f'<b>{"👤 کاربر" if item["sender_type"]=="USER" else "🛠 ادمین"}</b>\n{esc(item["message"])}\n\n'
    await edit(call,text,kb([[('💬 پاسخ',f'a_ticket_reply:{tid}'),('🔒 بستن',f'a_ticket_close:{tid}')],[('🔙 تیکت‌ها','a_tickets')]]))


@router.callback_query(F.data.startswith('a_ticket_reply:'))
async def a_ticket_reply_start(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); tid=int(call.data.split(':',1)[1]); await state.update_data(ticket_id=tid); await state.set_state(TicketReplyState.body); await call.message.answer('💬 پاسخ را بفرستید:')


@router.message(TicketReplyState.body)
async def a_ticket_reply_body(message: Message,state:FSMContext):
    data=await state.get_data(); await state.clear(); tid=int(data['ticket_id']); body=(message.text or '').strip()[:4000]
    if not is_admin(message.from_user.id): return
    ticket=await one('SELECT * FROM tickets WHERE id=? AND status<>"CLOSED"',(tid,))
    if not ticket: return await message.answer('❌ تیکت بسته/نامعتبر است.')
    await execute('''INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,message,telegram_message_id,created_at)
                     VALUES(?,?,?,?,?,?)''',(tid,'ADMIN',message.from_user.id,body,message.message_id,ts()))
    await execute('UPDATE tickets SET status="WAITING",assigned_admin_id=?,updated_at=? WHERE id=?',(message.from_user.id,ts(),tid))
    user=await one('SELECT telegram_id FROM users WHERE id=?',(ticket['user_id'],))
    try: await bot.send_message(user['telegram_id'],f'💬 پاسخ پشتیبانی در تیکت شما:\n\n{esc(body)}')
    except Exception: pass
    await audit(message.from_user.id,'REPLY_TICKET','TICKET',tid); await message.answer('✅ پاسخ ارسال شد.')


@router.callback_query(F.data.startswith('a_ticket_close:'))
async def a_ticket_close(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    tid=int(call.data.split(':',1)[1]); ticket=await one('SELECT * FROM tickets WHERE id=?',(tid,))
    if not ticket: return await call.answer('پیدا نشد.',show_alert=True)
    await execute('UPDATE tickets SET status="CLOSED",closed_at=?,updated_at=? WHERE id=?',(ts(),ts(),tid)); await audit(call.from_user.id,'CLOSE_TICKET','TICKET',tid); await call.answer('🔒 بسته شد'); await a_tickets(call)

# -------------------------------- admin users ---------------------------------

@router.callback_query(F.data == 'a_users')
async def a_users(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await rows('''SELECT u.telegram_id,u.username,u.first_name,u.is_blocked,w.balance
                       FROM users u LEFT JOIN wallets w ON w.user_id=u.id ORDER BY u.created_at DESC LIMIT 60''')
    buttons=[[(f'{"⛔" if u["is_blocked"] else "👤"} {u["first_name"] or "-"} | {u["telegram_id"]}',f'a_user:{u["telegram_id"]}')] for u in data]
    buttons += [[('🔎 جستجوی ID','a_lookup_user')],[('🔙 پنل','admin')]]
    await edit(call,'<b>👥 کاربران</b>\n\nجدیدترین کاربران:',kb(buttons))


@router.callback_query(F.data.startswith('a_user:'))
async def a_user(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); tg=int(call.data.split(':',1)[1]); user=await one('SELECT u.*,w.balance FROM users u LEFT JOIN wallets w ON w.user_id=u.id WHERE u.telegram_id=?',(tg,))
    if not user: return await call.answer('پیدا نشد.',show_alert=True)
    orders_count=int(await scalar('SELECT COUNT(*) FROM orders WHERE user_id=?',(user['id'],)) or 0)
    button=('✅ رفع مسدودی' if user['is_blocked'] else '⛔ مسدود')
    target=('a_unblock:' if user['is_blocked'] else 'a_block:')+str(tg)
    await edit(call,f'<b>👤 کاربر</b>\n\nID: <code>{tg}</code>\nUsername: @{esc(user["username"] or "-")}\nنام: {esc(user["first_name"] or "-")}\nکیف پول: {money(user["balance"])} {CURRENCY}\nسفارش‌ها: {orders_count}\nوضعیت: {"مسدود" if user["is_blocked"] else "فعال"}',
               kb([[(button,target)],[('🔙 کاربران','a_users')]]))


@router.callback_query(F.data == 'a_lookup_user')
async def a_lookup_user(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await state.set_state(UserLookupState.telegram_id); await call.message.answer('شناسه تلگرام را بفرستید:')


@router.message(UserLookupState.telegram_id)
async def lookup_user(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    await state.clear()
    try: tg=int((message.text or '').strip())
    except ValueError: return await message.answer('❌ شناسه نامعتبر.')
    if not await scalar('SELECT 1 FROM users WHERE telegram_id=?',(tg,)): return await message.answer('❌ کاربر پیدا نشد.')
    await message.answer('✅ کاربر پیدا شد.',reply_markup=kb([[('👤 مشاهده',f'a_user:{tg}')],[('🔙 پنل','admin')]]))


async def set_block(call: CallbackQuery, blocked: bool):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    tg=int(call.data.split(':',1)[1]); await execute('UPDATE users SET is_blocked=?,updated_at=? WHERE telegram_id=?',(1 if blocked else 0,ts(),tg)); await audit(call.from_user.id,'BLOCK_USER' if blocked else 'UNBLOCK_USER','USER',tg); await call.answer('✅ انجام شد'); await a_user(call)


@router.callback_query(F.data.startswith('a_block:'))
async def a_block(call: CallbackQuery): await set_block(call,True)

@router.callback_query(F.data.startswith('a_unblock:'))
async def a_unblock(call: CallbackQuery): await set_block(call,False)

# ------------------------------ broadcast/stats -------------------------------

@router.callback_query(F.data == 'a_broadcast')
async def a_broadcast(call: CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await state.set_state(BroadcastState.body); await call.message.answer('📢 متن پیام همگانی را بفرستید:')


@router.message(BroadcastState.body)
async def broadcast(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    text=(message.text or '').strip()[:4000]; await state.clear()
    users=await rows('SELECT telegram_id FROM users WHERE is_blocked=0')
    cur=await execute('INSERT INTO broadcasts(admin_id,message,status,total_users,created_at) VALUES(?,?,?,?,?)',(message.from_user.id,text,'RUNNING',len(users),ts()))
    sent=failed=0
    for i,user in enumerate(users):
        try: await bot.send_message(user['telegram_id'],text); sent+=1
        except Exception: failed+=1
        if i and i%20==0: await asyncio.sleep(0.5)
    await execute('UPDATE broadcasts SET status="COMPLETED",sent_count=?,failed_count=?,completed_at=? WHERE id=?',(sent,failed,ts(),cur.lastrowid))
    await audit(message.from_user.id,'BROADCAST','BROADCAST',cur.lastrowid,f'sent={sent},failed={failed}')
    await message.answer(f'✅ انجام شد.\nموفق: {sent}\nناموفق: {failed}')


@router.callback_query(F.data == 'a_referrals')
async def a_referrals(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await one('''SELECT COUNT(*) total,SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) completed,
                                           COALESCE(SUM(CASE WHEN status='COMPLETED' THEN reward_amount ELSE 0 END),0) rewards FROM referrals''')
    await edit(call,f'<b>🎁 دعوت‌ها</b>\n\nکل: {data["total"] or 0}\nموفق: {data["completed"] or 0}\nپاداش: {money(data["rewards"] or 0)} {CURRENCY}',back('admin'))


@router.callback_query(F.data == 'a_stats')
async def a_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await rows('''SELECT date(created_at) day,COUNT(*) orders,
                     SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) completed,
                     COALESCE(SUM(CASE WHEN status='COMPLETED' THEN payable_amount ELSE 0 END),0) revenue
                     FROM orders WHERE created_at>=date('now','-30 day') GROUP BY date(created_at) ORDER BY day DESC''')
    text='<b>📈 گزارش ۳۰ روزه</b>\n\n'
    for x in data: text += f'{x["day"]} — سفارش: {x["orders"]} | موفق: {x["completed"]} | درآمد: {money(x["revenue"])}\n'
    await edit(call,text or '📭 داده‌ای نیست.',back('admin'))


@router.callback_query(F.data == 'a_settings')
async def a_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); await edit(call,
        f'<b>⚙️ تنظیمات</b>\n\n🏪 {esc(SHOP_NAME)}\n💳 {esc(CARD_NUMBER)}\n👤 {esc(CARD_OWNER)}\n'
        f'🌐 درگاه: {"فعال" if PAYMENT_GATEWAY_URL else "خاموش"}\n🆘 پشتیبانی: @{esc(SUPPORT_USERNAME or "-")}\n'
        f'🎁 پاداش دعوت: {money(REFERRAL_REWARD)} {CURRENCY}\n⏰ مهلت سفارش: {ORDER_TTL_MINUTES} دقیقه\n⚠️ هشدار موجودی: {LOW_STOCK_THRESHOLD}',back('admin'))


@router.callback_query(F.data == 'a_logs')
async def a_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer('⛔',show_alert=True)
    await call.answer(); data=await rows('SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT 80')
    text='<b>🧾 لاگ ادمین</b>\n\n'
    for x in data: text += f'{esc(x["created_at"])} | <b>{esc(x["action"])}</b> | {esc(x["target_type"])} {esc(x["target_id"])}\n'
    await edit(call,text or '📭 لاگی نیست.',back('admin'))


# ------------------------------ admin commands --------------------------------

@router.message(Command('reply'))
async def reply_command(message: Message,state:FSMContext):
    if not is_admin(message.from_user.id): return await message.answer('⛔')
    parts=(message.text or '').split(maxsplit=1)
    if len(parts)<2 or not parts[1].isdigit(): return await message.answer('فرمت: /reply TICKET_ID')
    tid=int(parts[1])
    if not await scalar('SELECT 1 FROM tickets WHERE id=?',(tid,)): return await message.answer('❌ تیکت پیدا نشد.')
    await state.update_data(ticket_id=tid); await state.set_state(TicketReplyState.body); await message.answer('💬 پاسخ را بفرستید:')


@router.message(Command('backup'))
async def backup(message: Message):
    if not is_admin(message.from_user.id): return await message.answer('⛔')
    try:
        source=sqlite3.connect(DATABASE_PATH)
        filename=Path(DATABASE_PATH).stem+'_backup_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'.db'
        output=Path(DATABASE_PATH).with_name(filename)
        target=sqlite3.connect(output); source.backup(target); target.close(); source.close()
        data=output.read_bytes(); output.unlink(missing_ok=True)
        await message.answer_document(BufferedInputFile(data,filename=filename),caption='✅ بکاپ دیتابیس')
    except Exception:
        log.exception('backup failed'); await message.answer('❌ ساخت بکاپ ناموفق بود.')

# ---------------------------- background maintenance --------------------------

async def expiry_loop():
    while True:
        try:
            expired=await rows("""SELECT id,user_id,order_number FROM orders
                                 WHERE status IN ('PENDING_PAYMENT','AWAITING_RECEIPT') AND expires_at IS NOT NULL AND expires_at<?""",(ts(),))
            for order in expired:
                ok=await cancel_order_db(order['id'])
                if ok:
                    telegram_id=await scalar('SELECT telegram_id FROM users WHERE id=?',(order['user_id'],))
                    if telegram_id:
                        try: await bot.send_message(telegram_id,f'⌛ سفارش <code>{esc(order["order_number"])}</code> به دلیل پایان مهلت منقضی و رزروهای آن آزاد شد.')
                        except Exception: pass
            # Safety net: release stale reservations even if an order was deleted/manually changed.
            await execute('''UPDATE configs SET status='AVAILABLE',reserved_by_order_id=NULL,reserved_until=NULL
                             WHERE status='RESERVED' AND reserved_until IS NOT NULL AND reserved_until<?
                             AND reserved_by_order_id IN (SELECT id FROM orders WHERE status IN ('EXPIRED','CANCELLED','REJECTED'))''',(ts(),))
        except Exception:
            log.exception('expiry loop failed')
        await asyncio.sleep(20)


async def low_stock_loop():
    while True:
        try:
            if LOW_STOCK_THRESHOLD>0:
                products=await rows('SELECT id,name FROM products WHERE is_active=1')
                low=[]
                for product in products:
                    available=int(await scalar('SELECT COUNT(*) FROM configs WHERE product_id=? AND status="AVAILABLE"',(product['id'],)) or 0)
                    if available<=LOW_STOCK_THRESHOLD: low.append((product['name'],available))
                if low:
                    text='<b>⚠️ هشدار موجودی</b>\n\n' + '\n'.join(f'• {esc(name)}: <b>{count}</b>' for name,count in low)
                    for admin_id in ADMIN_IDS:
                        try: await bot.send_message(admin_id,text)
                        except Exception: pass
        except Exception:
            log.exception('stock loop failed')
        await asyncio.sleep(900)

# -------------------------------- webhook ------------------------------------

async def webhook_handler(request: web.Request):
    if request.match_info['secret'] != WEBHOOK_SECRET:
        return web.Response(status=404)
    try:
        payload=await request.json()
        update=Update.model_validate(payload,context={'bot':bot})
        await dp.feed_update(bot,update)
        return web.Response(text='ok')
    except Exception:
        log.exception('webhook error'); return web.Response(status=400,text='bad update')


async def start_webhook_server():
    app=web.Application(); app.router.add_post('/webhook/{secret}',webhook_handler)
    runner=web.AppRunner(app); await runner.setup(); await web.TCPSite(runner,'0.0.0.0',PORT).start()
    url=f'{WEBHOOK_BASE_URL}/webhook/{WEBHOOK_SECRET}'
    await bot.set_webhook(url); log.info('webhook set: %s',url)
    return runner


async def startup():
    await init_db()
    me=await bot.me(); log.info('started as @%s',me.username or '-')
    BACKGROUND.extend([asyncio.create_task(expiry_loop()),asyncio.create_task(low_stock_loop())])
    if WEBHOOK_BASE_URL:
        await start_webhook_server()


async def shutdown():
    for task in BACKGROUND: task.cancel()
    await asyncio.gather(*BACKGROUND,return_exceptions=True)
    if WEBHOOK_BASE_URL:
        try: await bot.delete_webhook()
        except Exception: pass
    await bot.session.close()


async def main():
    await startup()
    try:
        if WEBHOOK_BASE_URL:
            while True: await asyncio.sleep(3600)
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        await shutdown()


if __name__=='__main__':
    asyncio.run(main())
