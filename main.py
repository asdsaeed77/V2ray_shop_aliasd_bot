import os
import asyncio
import logging
import secrets
from datetime import datetime, timezone, timedelta

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update

# ============================================================
# V2RAY SHOP V2 - ONE FILE
# PostgreSQL + aiogram 3 + aiohttp / Render
#
# REQUIRED:
#   BOT_TOKEN
#   ADMIN_IDS=123456789,987654321
#   DATABASE_URL=postgresql://...
#
# OPTIONAL:
#   CARD_NUMBER
#   CARD_OWNER
#   PAYMENT_GATEWAY_URL
#   SUPPORT_USERNAME
#   SHOP_NAME
#   REFERRAL_REWARD
#   PORT
#
# Render Start Command:
#   python main.py
#
# Build Command:
#   pip install aiogram==3.25.0 aiohttp asyncpg
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000-0000-0000-0000").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "نام صاحب کارت").strip()
PAYMENT_GATEWAY_URL = os.getenv("PAYMENT_GATEWAY_URL", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
SHOP_NAME = os.getenv("SHOP_NAME", "V2RAY SHOP").strip()
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "10000"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("v2-shop")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
pool = None


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(x):
    return f"{int(x):,}"


def now():
    return datetime.now(timezone.utc)


def is_admin(uid):
    return uid in ADMIN_IDS


def markup(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=a, callback_data=b) for a, b in row]
            for row in rows
        ]
    )


def back(target="home"):
    return markup([[("🔙 بازگشت", target)]])


# ========================= DATABASE =========================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(255) DEFAULT '',
    first_name VARCHAR(255) DEFAULT '',
    last_name VARCHAR(255) DEFAULT '',
    language VARCHAR(20) DEFAULT 'fa',
    is_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admins (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(30) NOT NULL DEFAULT 'OWNER',
    permissions JSONB NOT NULL DEFAULT '{"all":true}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    slug VARCHAR(120) UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(200) UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    protocol VARCHAR(50) NOT NULL DEFAULT 'VLESS',
    price BIGINT NOT NULL DEFAULT 0 CHECK(price >= 0),
    currency VARCHAR(10) NOT NULL DEFAULT 'IRR',
    duration_days INTEGER NOT NULL DEFAULT 30,
    traffic_gb INTEGER,
    device_limit INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    order_number VARCHAR(40) UNIQUE NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id),
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING_PAYMENT',
    subtotal BIGINT NOT NULL DEFAULT 0,
    discount_amount BIGINT NOT NULL DEFAULT 0,
    wallet_amount BIGINT NOT NULL DEFAULT 0,
    payable_amount BIGINT NOT NULL DEFAULT 0,
    currency VARCHAR(10) NOT NULL DEFAULT 'IRR',
    discount_id BIGINT,
    payment_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS configs (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    config_text TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE'
      CHECK(status IN ('AVAILABLE','RESERVED','SOLD','DISABLED')),
    reserved_by_order_id BIGINT REFERENCES orders(id) ON DELETE SET NULL,
    reserved_until TIMESTAMPTZ,
    sold_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_configs_product_status ON configs(product_id,status);

CREATE TABLE IF NOT EXISTS order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price BIGINT NOT NULL,
    discount_amount BIGINT NOT NULL DEFAULT 0,
    total_price BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id),
    method VARCHAR(30) NOT NULL,
    amount BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    gateway VARCHAR(100),
    gateway_transaction_id VARCHAR(255),
    receipt_file_id TEXT,
    receipt_type VARCHAR(30),
    receipt_caption TEXT,
    reviewed_at TIMESTAMPTZ,
    reviewed_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discounts (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(100) UNIQUE NOT NULL,
    type VARCHAR(20) NOT NULL CHECK(type IN ('PERCENT','FIXED')),
    value BIGINT NOT NULL CHECK(value >= 0),
    minimum_order_amount BIGINT NOT NULL DEFAULT 0,
    maximum_discount BIGINT,
    usage_limit INTEGER,
    usage_count INTEGER NOT NULL DEFAULT 0,
    per_user_limit INTEGER NOT NULL DEFAULT 1,
    starts_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discount_usages (
    id BIGSERIAL PRIMARY KEY,
    discount_id BIGINT NOT NULL REFERENCES discounts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    amount BIGINT NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    balance BIGINT NOT NULL DEFAULT 0 CHECK(balance >= 0),
    currency VARCHAR(10) NOT NULL DEFAULT 'IRR',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id BIGSERIAL PRIMARY KEY,
    wallet_id BIGINT NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(30) NOT NULL,
    amount BIGINT NOT NULL,
    balance_before BIGINT NOT NULL,
    balance_after BIGINT NOT NULL,
    reference_type VARCHAR(50),
    reference_id VARCHAR(100),
    description TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS referrals (
    id BIGSERIAL PRIMARY KEY,
    referrer_user_id BIGINT NOT NULL REFERENCES users(id),
    referred_user_id BIGINT UNIQUE NOT NULL REFERENCES users(id),
    code VARCHAR(100) UNIQUE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    reward_amount BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tickets (
    id BIGSERIAL PRIMARY KEY,
    ticket_number VARCHAR(40) UNIQUE NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id),
    category VARCHAR(50) NOT NULL DEFAULT 'OTHER',
    subject VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    priority VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    assigned_admin_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender_type VARCHAR(20) NOT NULL,
    sender_id BIGINT NOT NULL,
    message TEXT NOT NULL,
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id BIGSERIAL PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    message TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    total_users INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id BIGSERIAL PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id VARCHAR(100),
    details TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
        for name, slug, desc in [
            ("VLESS", "vless", "کانفیگ‌های VLESS"),
            ("Trojan", "trojan", "کانفیگ‌های Trojan"),
            ("VMess", "vmess", "کانفیگ‌های VMess"),
            ("سایر", "other", "سایر محصولات"),
        ]:
            await con.execute(
                """INSERT INTO categories(name,slug,description)
                   VALUES($1,$2,$3) ON CONFLICT(slug) DO NOTHING""",
                name, slug, desc
            )


async def one(sql, *args):
    async with pool.acquire() as con:
        return await con.fetchrow(sql, *args)


async def rows(sql, *args):
    async with pool.acquire() as con:
        return await con.fetch(sql, *args)


async def value(sql, *args):
    async with pool.acquire() as con:
        return await con.fetchval(sql, *args)


async def execute(sql, *args):
    async with pool.acquire() as con:
        return await con.execute(sql, *args)


async def ensure_user(tg):
    async with pool.acquire() as con:
        user = await con.fetchrow(
            """INSERT INTO users(telegram_id,username,first_name,last_name,language)
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT(telegram_id) DO UPDATE SET
               username=EXCLUDED.username, first_name=EXCLUDED.first_name,
               last_name=EXCLUDED.last_name, language=EXCLUDED.language,
               updated_at=NOW()
               RETURNING *""",
            tg.id, tg.username or "", tg.first_name or "",
            tg.last_name or "", tg.language_code or "fa"
        )
        await con.execute(
            "INSERT INTO wallets(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING",
            user["id"]
        )
        if tg.id in ADMIN_IDS:
            await con.execute(
                """INSERT INTO admins(user_id,role)
                   VALUES($1,'OWNER')
                   ON CONFLICT(user_id) DO UPDATE SET is_active=TRUE,role='OWNER'""",
                user["id"]
            )
        return user


async def admin_log(uid, action, target_type="", target_id="", details=""):
    await execute(
        """INSERT INTO admin_logs(admin_id,action,target_type,target_id,details)
           VALUES($1,$2,$3,$4,$5)""",
        uid, action, target_type, target_id, details
    )


# ========================= FSM =========================

class AddProduct(StatesGroup):
    name = State()
    category = State()
    protocol = State()
    price = State()
    duration = State()
    traffic = State()
    devices = State()
    description = State()


class AddConfig(StatesGroup):
    text = State()


class BulkConfig(StatesGroup):
    text = State()


class AddDiscount(StatesGroup):
    code = State()
    type = State()
    value = State()
    minimum = State()
    limit = State()
    expires = State()


class Ticket(StatesGroup):
    subject = State()
    message = State()


class Broadcast(StatesGroup):
    message = State()


class WalletDeposit(StatesGroup):
    amount = State()


# ========================= HOME =========================

def home_keyboard(admin=False):
    r = [
        [("🛍 فروشگاه", "shop"), ("📦 سفارش‌های من", "orders")],
        [("👤 حساب کاربری", "account"), ("💰 کیف پول", "wallet")],
        [("🎟 کد تخفیف", "discount"), ("🎁 دعوت دوستان", "referral")],
        [("🆘 پشتیبانی", "support"), ("💎 راهنما", "guide")],
    ]
    if admin:
        r.append([("⚙️ پنل مدیریت", "admin")])
    return markup(r)


async def send_home(message):
    u = await ensure_user(message.from_user)
    w = await one("SELECT balance FROM wallets WHERE user_id=$1", u["id"])
    await message.answer(
        f"<b>✨ {esc(SHOP_NAME)}</b>\n\n"
        "به فروشگاه خوش آمدید 👋\n\n"
        "🔐 VLESS • Trojan • VMess\n"
        "⚡ تحویل سریع\n"
        "🛡️ فروش امن\n"
        "🎫 پشتیبانی\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 موجودی کیف پول: <b>{money(w['balance'])} تومان</b>",
        reply_markup=home_keyboard(is_admin(message.from_user.id))
    )


@router.message(CommandStart())
async def start(message: Message):
    await ensure_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        await handle_referral(message.from_user.id, parts[1][4:])
    await send_home(message)


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    await admin_home(message)


@router.callback_query(F.data == "home")
async def home_cb(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"<b>✨ {esc(SHOP_NAME)}</b>\n\nبه فروشگاه خوش آمدید 👋",
        reply_markup=home_keyboard(is_admin(call.from_user.id))
    )


# ========================= SHOP =========================

@router.callback_query(F.data == "shop")
async def shop(call: CallbackQuery):
    await call.answer()
    cats = await rows("SELECT * FROM categories WHERE is_active=TRUE ORDER BY sort_order,id")
    buttons = [[(f"🔹 {c['name']}", f"cat:{c['id']}")] for c in cats]
    buttons.append([("🔥 همه محصولات", "all_products")])
    buttons.append([("🔙 بازگشت", "home")])
    await call.message.edit_text(
        "<b>🛍 فروشگاه</b>\n\nدسته‌بندی را انتخاب کنید:",
        reply_markup=markup(buttons)
    )


async def product_list_text(call, condition_sql, args, title, back_target):
    ps = await rows(
        f"""SELECT p.*,COALESCE(COUNT(c.id) FILTER(WHERE c.status='AVAILABLE'),0) stock
            FROM products p LEFT JOIN configs c ON c.product_id=p.id
            WHERE p.is_active=TRUE AND {condition_sql}
            GROUP BY p.id ORDER BY p.sort_order,p.id DESC""",
        *args
    )
    if not ps:
        return await call.message.edit_text("📭 محصولی موجود نیست.", reply_markup=back(back_target))
    buttons = [[(f"{p['name']} | {money(p['price'])} تومان", f"product:{p['id']}")] for p in ps]
    buttons.append([("🔙 بازگشت", back_target)])
    await call.message.edit_text(f"<b>{title}</b>\n\nمحصول را انتخاب کنید:", reply_markup=markup(buttons))


@router.callback_query(F.data == "all_products")
async def all_products(call: CallbackQuery):
    await call.answer()
    await product_list_text(call, "1=1", (), "🛍 همه محصولات", "shop")


@router.callback_query(F.data.startswith("cat:"))
async def category(call: CallbackQuery):
    await call.answer()
    cid = int(call.data.split(":")[1])
    await product_list_text(call, "p.category_id=$1", (cid,), "🛍 محصولات", "shop")


@router.callback_query(F.data.startswith("product:"))
async def product_detail(call: CallbackQuery):
    await call.answer()
    pid = int(call.data.split(":")[1])
    p = await one(
        """SELECT p.*,COALESCE(COUNT(c.id) FILTER(WHERE c.status='AVAILABLE'),0) stock
           FROM products p LEFT JOIN configs c ON c.product_id=p.id
           WHERE p.id=$1 AND p.is_active=TRUE GROUP BY p.id""",
        pid
    )
    if not p:
        return await call.message.edit_text("❌ محصول پیدا نشد.", reply_markup=back("shop"))

    traffic = "نامحدود" if p["traffic_gb"] is None else f"{p['traffic_gb']} GB"
    devices = "نامحدود" if p["device_limit"] is None else p["device_limit"]
    text = (
        f"<b>{esc(p['name'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 نوع: {esc(p['protocol'])}\n"
        f"⏳ اعتبار: {p['duration_days']} روز\n"
        f"📊 حجم: {traffic}\n"
        f"📱 دستگاه: {devices}\n"
        f"📦 موجودی: {p['stock']}\n"
        f"💰 قیمت: <b>{money(p['price'])} تومان</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{esc(p['description'] or 'توضیحی ثبت نشده است.')}"
    )
    buttons = []
    if p["stock"] > 0:
        buttons.append([("🛒 خرید", f"buy:{pid}")])
    else:
        text += "\n\n⛔ این محصول ناموجود است."
    buttons.append([("🔙 محصولات", "shop")])
    await call.message.edit_text(text, reply_markup=markup(buttons))


# ========================= ORDER =========================

async def create_order(tg_id, pid):
    async with pool.acquire() as con:
        async with con.transaction():
            u = await con.fetchrow("SELECT id FROM users WHERE telegram_id=$1", tg_id)
            p = await con.fetchrow("SELECT * FROM products WHERE id=$1 AND is_active=TRUE", pid)
            if not u or not p:
                return None
            stock = await con.fetchval(
                "SELECT COUNT(*) FROM configs WHERE product_id=$1 AND status='AVAILABLE'",
                pid
            )
            if not stock:
                return None
            number = "V2-" + secrets.token_hex(4).upper()
            order = await con.fetchrow(
                """INSERT INTO orders(order_number,user_id,subtotal,payable_amount,expires_at)
                   VALUES($1,$2,$3,$3,NOW()+INTERVAL '30 minutes') RETURNING *""",
                number, u["id"], p["price"]
            )
            await con.execute(
                """INSERT INTO order_items(order_id,product_id,quantity,unit_price,total_price)
                   VALUES($1,$2,1,$3,$3)""",
                order["id"], pid, p["price"]
            )
            return order, p


@router.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery):
    await call.answer()
    result = await create_order(call.from_user.id, int(call.data.split(":")[1]))
    if not result:
        return await call.message.edit_text("⛔ محصول موجود نیست.", reply_markup=back("shop"))
    order, p = result
    buttons = []
    if PAYMENT_GATEWAY_URL:
        buttons.append([("🌐 پرداخت آنلاین", f"gateway:{order['id']}")])
    buttons += [
        [("💳 کارت‌به‌کارت", f"card:{order['id']}")],
        [("💰 پرداخت با کیف پول", f"walletpay:{order['id']}")],
        [("❌ لغو سفارش", f"cancel:{order['id']}")],
    ]
    await call.message.edit_text(
        "💳 <b>پرداخت سفارش</b>\n\n"
        f"🧾 سفارش: <code>{order['order_number']}</code>\n"
        f"📦 محصول: {esc(p['name'])}\n"
        f"💰 مبلغ: <b>{money(order['payable_amount'])} تومان</b>\n\n"
        "روش پرداخت را انتخاب کنید:",
        reply_markup=markup(buttons)
    )


@router.callback_query(F.data.startswith("card:"))
async def card_payment(call: CallbackQuery):
    await call.answer()
    oid = int(call.data.split(":")[1])
    order = await one(
        """SELECT o.*,p.name FROM orders o
           JOIN order_items oi ON oi.order_id=o.id
           JOIN products p ON p.id=oi.product_id
           WHERE o.id=$1 AND o.user_id=(SELECT id FROM users WHERE telegram_id=$2)""",
        oid, call.from_user.id
    )
    if not order:
        return await call.message.edit_text("❌ سفارش پیدا نشد.", reply_markup=back())

    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "UPDATE orders SET status='AWAITING_RECEIPT',updated_at=NOW() WHERE id=$1",
                oid
            )
            await con.execute(
                """INSERT INTO payments(order_id,user_id,method,amount,status)
                   VALUES($1,(SELECT id FROM users WHERE telegram_id=$2),'CARD_TO_CARD',$3,'PENDING')""",
                oid, call.from_user.id, order["payable_amount"]
            )

    await call.message.edit_text(
        "💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
        f"🧾 سفارش: <code>{order['order_number']}</code>\n"
        f"💰 مبلغ: <b>{money(order['payable_amount'])} تومان</b>\n\n"
        f"شماره کارت:\n<code>{esc(CARD_NUMBER)}</code>\n"
        f"👤 به نام: <b>{esc(CARD_OWNER)}</b>\n\n"
        "بعد از پرداخت، رسید را به صورت عکس یا فایل همین‌جا ارسال کنید.",
        reply_markup=markup([
            [("❌ لغو سفارش", f"cancel:{oid}")],
            [("🔙 خانه", "home")]
        ])
    )


@router.callback_query(F.data.startswith("gateway:"))
async def gateway(call: CallbackQuery):
    await call.answer()
    oid = int(call.data.split(":")[1])
    order = await one(
        "SELECT * FROM orders WHERE id=$1 AND user_id=(SELECT id FROM users WHERE telegram_id=$2)",
        oid, call.from_user.id
    )
    if not order:
        return
    await execute(
        """INSERT INTO payments(order_id,user_id,method,amount,status,gateway)
           VALUES($1,(SELECT id FROM users WHERE telegram_id=$2),'GATEWAY',$3,'PENDING',$4)""",
        oid, call.from_user.id, order["payable_amount"], PAYMENT_GATEWAY_URL
    )
    await call.message.edit_text(
        "🌐 <b>پرداخت آنلاین</b>\n\nبرای پرداخت روی لینک زیر بزنید:",
        reply_markup=markup([
            [("💳 ورود به درگاه", "show_gateway")],
            [("📦 سفارش‌های من", "orders")],
            [("🔙 خانه", "home")]
        ])
    )


@router.callback_query(F.data == "show_gateway")
async def show_gateway(call: CallbackQuery):
    await call.answer()
    if PAYMENT_GATEWAY_URL:
        await call.message.answer(f"🌐 <a href='{esc(PAYMENT_GATEWAY_URL)}'>ورود به درگاه پرداخت</a>")
    else:
        await call.message.answer("درگاه تنظیم نشده است.")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(call: CallbackQuery):
    await call.answer()
    oid = int(call.data.split(":")[1])
    await execute(
        """UPDATE orders SET status='CANCELLED',updated_at=NOW()
           WHERE id=$1 AND user_id=(SELECT id FROM users WHERE telegram_id=$2)
           AND status IN ('PENDING_PAYMENT','AWAITING_RECEIPT')""",
        oid, call.from_user.id
    )
    await call.message.edit_text("🚫 سفارش لغو شد.", reply_markup=home_keyboard(is_admin(call.from_user.id)))


# ========================= RECEIPTS =========================

@router.message(F.photo)
async def photo_receipt(message: Message):
    await ensure_user(message.from_user)
    await receive_receipt(message, message.photo[-1].file_id, "PHOTO")


@router.message(F.document)
async def document_receipt(message: Message):
    await ensure_user(message.from_user)
    await receive_receipt(message, message.document.file_id, "DOCUMENT")


async def receive_receipt(message, file_id, typ):
    u = await one("SELECT id FROM users WHERE telegram_id=$1", message.from_user.id)
    o = await one(
        """SELECT o.*,p.name FROM orders o
           JOIN order_items oi ON oi.order_id=o.id
           JOIN products p ON p.id=oi.product_id
           WHERE o.user_id=$1 AND o.status='AWAITING_RECEIPT'
           ORDER BY o.created_at DESC LIMIT 1""",
        u["id"]
    )
    if not o:
        return await message.answer("❗ سفارش در انتظار رسید پیدا نشد.")

    pay = await one("SELECT * FROM payments WHERE order_id=$1 ORDER BY id DESC LIMIT 1", o["id"])
    if pay:
        await execute(
            """UPDATE payments SET status='SUBMITTED',receipt_file_id=$1,
               receipt_type=$2,receipt_caption=$3,updated_at=NOW() WHERE id=$4""",
            file_id, typ, message.caption or "", pay["id"]
        )
    else:
        await execute(
            """INSERT INTO payments(order_id,user_id,method,amount,status,receipt_file_id,receipt_type)
               VALUES($1,$2,'CARD_TO_CARD',$3,'SUBMITTED',$4,$5)""",
            o["id"], u["id"], o["payable_amount"], file_id, typ
        )

    await execute(
        "UPDATE orders SET status='PAYMENT_REVIEW',updated_at=NOW() WHERE id=$1",
        o["id"]
    )

    admin_buttons = markup([[
        ("✅ تأیید و تحویل", f"approve:{o['id']}"),
        ("❌ رد پرداخت", f"reject:{o['id']}")
    ]])
    caption = (
        f"🧾 <b>رسید جدید</b>\n\n"
        f"سفارش: <code>{o['order_number']}</code>\n"
        f"محصول: {esc(o['name'])}\n"
        f"مبلغ: <b>{money(o['payable_amount'])} تومان</b>\n"
        f"کاربر: <code>{message.from_user.id}</code>\n"
        f"@{esc(message.from_user.username or '-')}"
    )
    for aid in ADMIN_IDS:
        try:
            if typ == "PHOTO":
                await bot.send_photo(aid, file_id, caption=caption, reply_markup=admin_buttons)
            else:
                await bot.send_document(aid, file_id, caption=caption, reply_markup=admin_buttons)
        except Exception:
            log.exception("Admin notification failed")

    await message.answer(
        f"📨 رسید سفارش <code>{o['order_number']}</code> دریافت شد.\n"
        "⏳ منتظر بررسی ادمین باشید."
    )


# ========================= DELIVERY =========================

async def claim_config(con, order_id):
    item = await con.fetchrow(
        "SELECT product_id FROM order_items WHERE order_id=$1 ORDER BY id LIMIT 1",
        order_id
    )
    if not item:
        return None

    cfg = await con.fetchrow(
        """SELECT * FROM configs
           WHERE product_id=$1 AND status='AVAILABLE'
           ORDER BY id
           FOR UPDATE SKIP LOCKED LIMIT 1""",
        item["product_id"]
    )
    if not cfg:
        return None

    updated = await con.fetchrow(
        """UPDATE configs SET status='SOLD',reserved_by_order_id=$1,
           sold_at=NOW() WHERE id=$2 AND status='AVAILABLE'
           RETURNING config_text""",
        order_id, cfg["id"]
    )
    return updated["config_text"] if updated else None


async def deliver(tg_id, config_text, order_number):
    await bot.send_message(
        tg_id,
        f"🎉 <b>پرداخت تأیید شد!</b>\n\n"
        f"🧾 سفارش: <code>{order_number}</code>\n\n"
        "🔐 <b>کانفیگ شما:</b>\n"
        f"<pre>{esc(config_text)}</pre>\n\n"
        "⚠️ این کانفیگ به عنوان فروخته‌شده ثبت شد."
    )


@router.callback_query(F.data.startswith("approve:"))
async def approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    oid = int(call.data.split(":")[1])

    async with pool.acquire() as con:
        async with con.transaction():
            order = await con.fetchrow("SELECT * FROM orders WHERE id=$1 FOR UPDATE", oid)
            if not order or order["status"] != "PAYMENT_REVIEW":
                return await call.answer("این سفارش قابل تأیید نیست.", show_alert=True)

            pay = await con.fetchrow(
                "SELECT * FROM payments WHERE order_id=$1 ORDER BY id DESC LIMIT 1 FOR UPDATE",
                oid
            )
            config = await claim_config(con, oid)
            if not config:
                return await call.answer("⛔ کانفیگ موجود نیست؛ پرداخت تأیید نشد.", show_alert=True)

            await con.execute(
                """UPDATE payments SET status='VERIFIED',reviewed_at=NOW(),
                   reviewed_by=$1,updated_at=NOW() WHERE id=$2""",
                call.from_user.id, pay["id"]
            )
            await con.execute(
                """UPDATE orders SET status='COMPLETED',payment_status='PAID',updated_at=NOW()
                   WHERE id=$1""",
                oid
            )
            user = await con.fetchrow(
                "SELECT telegram_id FROM users WHERE id=$1", order["user_id"]
            )

    await admin_log(call.from_user.id, "APPROVE_PAYMENT", "ORDER", str(oid))
    try:
        await deliver(user["telegram_id"], config, order["order_number"])
    except Exception:
        log.exception("Delivery failed")

    await call.answer("✅ تأیید و تحویل شد.")
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("reject:"))
async def reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    oid = int(call.data.split(":")[1])

    async with pool.acquire() as con:
        async with con.transaction():
            order = await con.fetchrow("SELECT * FROM orders WHERE id=$1 FOR UPDATE", oid)
            if not order or order["status"] != "PAYMENT_REVIEW":
                return await call.answer("این سفارش قابل رد نیست.", show_alert=True)
            pay = await con.fetchrow(
                "SELECT * FROM payments WHERE order_id=$1 ORDER BY id DESC LIMIT 1 FOR UPDATE",
                oid
            )
            if pay:
                await con.execute(
                    """UPDATE payments SET status='REJECTED',reviewed_at=NOW(),
                       reviewed_by=$1,updated_at=NOW() WHERE id=$2""",
                    call.from_user.id, pay["id"]
                )
            await con.execute(
                """UPDATE orders SET status='REJECTED',payment_status='REJECTED',updated_at=NOW()
                   WHERE id=$1""",
                oid
            )
            user = await con.fetchrow(
                "SELECT telegram_id FROM users WHERE id=$1", order["user_id"]
            )

    await admin_log(call.from_user.id, "REJECT_PAYMENT", "ORDER", str(oid))
    try:
        await bot.send_message(
            user["telegram_id"],
            f"❌ <b>سفارش {order['order_number']} تأیید نشد.</b>\n\n"
            "اگر فکر می‌کنید اشتباهی رخ داده، با پشتیبانی تماس بگیرید."
        )
    except Exception:
        pass
    await call.answer("❌ پرداخت رد شد.")
    await call.message.edit_reply_markup(reply_markup=None)


# ========================= ORDERS / ACCOUNT =========================

@router.callback_query(F.data == "orders")
async def my_orders(call: CallbackQuery):
    await call.answer()
    rs = await rows(
        """SELECT o.*,p.name FROM orders o
           JOIN order_items oi ON oi.order_id=o.id
           JOIN products p ON p.id=oi.product_id
           WHERE o.user_id=(SELECT id FROM users WHERE telegram_id=$1)
           ORDER BY o.created_at DESC LIMIT 20""",
        call.from_user.id
    )
    if not rs:
        return await call.message.edit_text("📭 سفارشی ندارید.", reply_markup=back())
    status = {
        "PENDING_PAYMENT": "💳 در انتظار پرداخت",
        "AWAITING_RECEIPT": "📎 منتظر رسید",
        "PAYMENT_REVIEW": "🔎 در انتظار بررسی",
        "COMPLETED": "🎉 تکمیل شده",
        "REJECTED": "❌ رد شده",
        "CANCELLED": "🚫 لغو شده",
        "EXPIRED": "⌛ منقضی"
    }
    text = "<b>📦 سفارش‌های من</b>\n\n"
    for r in rs:
        text += (
            f"<code>{r['order_number']}</code> — {esc(r['name'])}\n"
            f"{money(r['payable_amount'])} تومان — {status.get(r['status'],r['status'])}\n\n"
        )
    await call.message.edit_text(text, reply_markup=back())


@router.callback_query(F.data == "account")
async def account(call: CallbackQuery):
    await call.answer()
    u = await one("SELECT * FROM users WHERE telegram_id=$1", call.from_user.id)
    w = await one("SELECT balance FROM wallets WHERE user_id=$1", u["id"])
    s = await one(
        """SELECT COUNT(*) total,COUNT(*) FILTER(WHERE status='COMPLETED') completed
           FROM orders WHERE user_id=$1""",
        u["id"]
    )
    await call.message.edit_text(
        "<b>👤 حساب کاربری</b>\n\n"
        f"🆔 <code>{u['telegram_id']}</code>\n"
        f"👤 @{esc(u['username'] or '-')}\n"
        f"📦 سفارش‌ها: {s['total']}\n"
        f"✅ خرید موفق: {s['completed']}\n"
        f"💰 کیف پول: <b>{money(w['balance'])} تومان</b>",
        reply_markup=markup([
            [("📦 سفارش‌ها", "orders"), ("💰 کیف پول", "wallet")],
            [("🎁 دعوت دوستان", "referral")],
            [("🔙 خانه", "home")]
        ])
    )


# ========================= WALLET =========================

@router.callback_query(F.data == "wallet")
async def wallet(call: CallbackQuery):
    await call.answer()
    u = await one("SELECT id FROM users WHERE telegram_id=$1", call.from_user.id)
    w = await one("SELECT * FROM wallets WHERE user_id=$1", u["id"])
    ts = await rows(
        "SELECT * FROM wallet_transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5",
        u["id"]
    )
    text = f"<b>💰 کیف پول</b>\n\nموجودی: <b>{money(w['balance'])} تومان</b>\n\n"
    if ts:
        text += "<b>آخرین تراکنش‌ها:</b>\n"
        for t in ts:
            sign = "+" if t["type"] in ("DEPOSIT","REFUND","REFERRAL_REWARD") else "-"
            text += f"{sign}{money(t['amount'])} — {esc(t['description'])}\n"
    await call.message.edit_text(
        text,
        reply_markup=markup([
            [("➕ افزایش موجودی", "wallet_add")],
            [("📜 تراکنش‌ها", "wallet_tx")],
            [("🔙 بازگشت", "home")]
        ])
    )


@router.callback_query(F.data == "wallet_add")
async def wallet_add(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(WalletDeposit.amount)
    await call.message.answer("مبلغ افزایش موجودی به تومان را بفرست:")


@router.message(WalletDeposit.amount)
async def wallet_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.replace(",", "").replace("٬", "").strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        return await message.answer("❌ مبلغ نامعتبر است.")
    await state.clear()
    await message.answer(
        f"💰 مبلغ: <b>{money(amount)} تومان</b>\n\n"
        f"💳 کارت:\n<code>{esc(CARD_NUMBER)}</code>\n"
        f"👤 به نام: {esc(CARD_OWNER)}\n\n"
        "رسید را ارسال کنید.\n"
        "تأیید افزایش موجودی در نسخه بعدی پنل پرداخت انجام می‌شود."
    )


@router.callback_query(F.data == "wallet_tx")
async def wallet_tx(call: CallbackQuery):
    await call.answer()
    u = await one("SELECT id FROM users WHERE telegram_id=$1", call.from_user.id)
    ts = await rows(
        "SELECT * FROM wallet_transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30",
        u["id"]
    )
    text = "<b>📜 تراکنش‌های کیف پول</b>\n\n"
    for t in ts:
        sign = "+" if t["type"] in ("DEPOSIT","REFUND","REFERRAL_REWARD") else "-"
        text += f"{sign}{money(t['amount'])} — {esc(t['description'])}\n"
    await call.message.edit_text(text or "تراکنشی نیست.", reply_markup=back("wallet"))


# ========================= REFERRAL =========================

async def handle_referral(tg_id, code):
    if not code.startswith("U"):
        return
    try:
        ref_user_id = int(code[1:])
    except ValueError:
        return
    async with pool.acquire() as con:
        target = await con.fetchrow("SELECT id FROM users WHERE telegram_id=$1", tg_id)
        referrer = await con.fetchrow("SELECT id FROM users WHERE id=$1", ref_user_id)
        if not target or not referrer or target["id"] == referrer["id"]:
            return
        await con.execute(
            """INSERT INTO referrals(
                 referrer_user_id,referred_user_id,code,reward_amount
               ) VALUES($1,$2,$3,$4)
               ON CONFLICT(referred_user_id) DO NOTHING""",
            referrer["id"], target["id"], f"USED-{tg_id}", REFERRAL_REWARD
        )


@router.callback_query(F.data == "referral")
async def referral(call: CallbackQuery):
    await call.answer()
    u = await one("SELECT id FROM users WHERE telegram_id=$1", call.from_user.id)
    link = f"https://t.me/{(await bot.me()).username}?start=ref_U{u['id']}"
    count = await value(
        "SELECT COUNT(*) FROM referrals WHERE referrer_user_id=$1 AND referred_user_id<>$1",
        u["id"]
    )
    reward = await value(
        """SELECT COALESCE(SUM(reward_amount),0) FROM referrals
           WHERE referrer_user_id=$1 AND status='COMPLETED'""",
        u["id"]
    )
    await call.message.edit_text(
        "<b>🎁 دعوت دوستان</b>\n\n"
        f"👥 دعوت‌ها: {count}\n"
        f"💰 پاداش دریافت‌شده: {money(reward)} تومان\n\n"
        f"🔗 لینک دعوت:\n<code>{link}</code>\n\n"
        f"🎁 پاداش هر دعوت: {money(REFERRAL_REWARD)} تومان",
        reply_markup=back()
    )


@router.callback_query(F.data == "discount")
async def discount(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "<b>🎟 کد تخفیف</b>\n\n"
        "کدهای تخفیف توسط ادمین ساخته می‌شوند.\n"
        "در مرحله پرداخت می‌توانیم ورود و محاسبه خودکار کد را فعال کنیم.",
        reply_markup=back()
    )


# ========================= SUPPORT =========================

@router.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "<b>🆘 پشتیبانی</b>\n\nموضوع را انتخاب کنید:",
        reply_markup=markup([
            [("💳 پرداخت", "ticket:PAYMENT"), ("📦 سفارش", "ticket:ORDER")],
            [("🔐 کانفیگ", "ticket:CONFIG"), ("❓ سایر", "ticket:OTHER")],
            [("🔙 بازگشت", "home")]
        ])
    )


@router.callback_query(F.data.startswith("ticket:"))
async def ticket_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(category=call.data.split(":")[1])
    await state.set_state(Ticket.subject)
    await call.message.answer("موضوع تیکت را بنویس:")


@router.message(Ticket.subject)
async def ticket_subject(message: Message, state: FSMContext):
    await state.update_data(subject=(message.text or "")[:255])
    await state.set_state(Ticket.message)
    await message.answer("پیام خود را بنویس:")


@router.message(Ticket.message)
async def ticket_message(message: Message, state: FSMContext):
    data = await state.get_data()
    u = await one("SELECT id FROM users WHERE telegram_id=$1", message.from_user.id)
    number = "T-" + secrets.token_hex(4).upper()
    async with pool.acquire() as con:
        t = await con.fetchrow(
            """INSERT INTO tickets(ticket_number,user_id,category,subject)
               VALUES($1,$2,$3,$4) RETURNING *""",
            number, u["id"], data["category"], data["subject"]
        )
        await con.execute(
            """INSERT INTO ticket_messages(
               ticket_id,sender_type,sender_id,message,telegram_message_id)
               VALUES($1,'USER',$2,$3,$4)""",
            t["id"], u["id"], message.text, message.message_id
        )
    await state.clear()
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid,
                f"🆘 <b>تیکت جدید {number}</b>\n\n"
                f"موضوع: {esc(data['subject'])}\n"
                f"کاربر: <code>{message.from_user.id}</code>\n\n"
                f"{esc(message.text)}"
            )
        except Exception:
            pass
    await message.answer(
        f"✅ تیکت <code>{number}</code> ثبت شد.",
        reply_markup=home_keyboard(is_admin(message.from_user.id))
    )


@router.callback_query(F.data == "guide")
async def guide(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "<b>💎 راهنمای خرید</b>\n\n"
        "1️⃣ محصول را انتخاب کنید.\n"
        "2️⃣ سفارش بسازید.\n"
        "3️⃣ پرداخت کنید.\n"
        "4️⃣ رسید را بفرستید.\n"
        "5️⃣ ادمین پرداخت را بررسی می‌کند.\n"
        "6️⃣ بعد از تأیید، یک کانفیگ موجود و استفاده‌نشده تحویل می‌شود.\n\n"
        "هر کانفیگ فقط یک بار فروخته می‌شود.",
        reply_markup=back()
    )


# ========================= ADMIN =========================

async def admin_home(target):
    s = await one(
        """SELECT
          (SELECT COUNT(*) FROM users) users,
          (SELECT COUNT(*) FROM orders) orders,
          (SELECT COUNT(*) FROM orders WHERE status='COMPLETED') completed,
          (SELECT COALESCE(SUM(payable_amount),0) FROM orders WHERE status='COMPLETED') revenue,
          (SELECT COUNT(*) FROM configs WHERE status='AVAILABLE') available"""
    )
    await target.answer(
        "<b>⚙️ پنل مدیریت</b>\n\n"
        f"👥 کاربران: {s['users']}\n"
        f"🛒 سفارش‌ها: {s['orders']}\n"
        f"✅ فروش موفق: {s['completed']}\n"
        f"💰 فروش: {money(s['revenue'])} تومان\n"
        f"📦 موجودی: {s['available']}",
        reply_markup=markup([
            [("🛍 محصولات", "a_products"), ("🔐 کانفیگ‌ها", "a_configs")],
            [("🧾 سفارش‌ها", "a_orders"), ("👥 کاربران", "a_users")],
            [("💳 پرداخت‌ها", "a_payments"), ("🎟 تخفیف‌ها", "a_discounts")],
            [("💰 کیف پول", "a_wallet"), ("🎁 دعوت‌ها", "a_referrals")],
            [("🆘 تیکت‌ها", "a_tickets"), ("📢 پیام همگانی", "a_broadcast")],
            [("📊 گزارش‌ها", "a_stats"), ("⚙️ تنظیمات", "a_settings")],
            [("🔙 خانه", "home")]
        ])
    )


@router.callback_query(F.data == "admin")
async def admin_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    await admin_home(call.message)


@router.callback_query(F.data == "a_products")
async def a_products(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    ps = await rows(
        """SELECT p.*,COALESCE(COUNT(c.id) FILTER(WHERE c.status='AVAILABLE'),0) stock
           FROM products p LEFT JOIN configs c ON c.product_id=p.id
           GROUP BY p.id ORDER BY p.id DESC"""
    )
    buttons = [[("➕ افزودن محصول", "a_add_product")]]
    buttons += [[(f"{'🟢' if p['is_active'] else '⚪'} {p['name']}", f"a_product:{p['id']}")] for p in ps]
    buttons.append([("🔙 پنل", "admin")])
    await call.message.edit_text("<b>🛍 مدیریت محصولات</b>", reply_markup=markup(buttons))


@router.callback_query(F.data == "a_add_product")
async def a_add_product(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    await state.set_state(AddProduct.name)
    await call.message.answer("نام محصول:")


@router.message(AddProduct.name)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    cats = await rows("SELECT id,name FROM categories WHERE is_active=TRUE ORDER BY id")
    await state.set_state(AddProduct.category)
    await message.answer(
        "دسته‌بندی:",
        reply_markup=markup([[(c["name"], f"pickcat:{c['id']}")] for c in cats])
    )


@router.callback_query(F.data.startswith("pickcat:"))
async def pick_cat(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await state.update_data(category_id=int(call.data.split(":")[1]))
    await state.set_state(AddProduct.protocol)
    await call.answer()
    await call.message.answer("پروتکل: VLESS / Trojan / VMess / ...")


@router.message(AddProduct.protocol)
async def add_protocol(message: Message, state: FSMContext):
    await state.update_data(protocol=message.text.strip())
    await state.set_state(AddProduct.price)
    await message.answer("قیمت به تومان:")


@router.message(AddProduct.price)
async def add_price(message: Message, state: FSMContext):
    try:
        n = int(message.text.replace(",", "").replace("٬", "").strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ قیمت نامعتبر.")
    await state.update_data(price=n)
    await state.set_state(AddProduct.duration)
    await message.answer("اعتبار به روز:")


@router.message(AddProduct.duration)
async def add_duration(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n <= 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(duration=n)
    await state.set_state(AddProduct.traffic)
    await message.answer("حجم به GB؛ عدد 0 یعنی نامحدود:")


@router.message(AddProduct.traffic)
async def add_traffic(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(traffic=None if n == 0 else n)
    await state.set_state(AddProduct.devices)
    await message.answer("تعداد دستگاه؛ 0 یعنی نامحدود:")


@router.message(AddProduct.devices)
async def add_devices(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(devices=None if n == 0 else n)
    await state.set_state(AddProduct.description)
    await message.answer("توضیحات؛ برای خالی بودن - بفرست:")


@router.message(AddProduct.description)
async def add_description(message: Message, state: FSMContext):
    d = await state.get_data()
    desc = "" if message.text.strip() == "-" else message.text.strip()
    await execute(
        """INSERT INTO products(
           category_id,name,slug,description,protocol,price,duration_days,traffic_gb,device_limit)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        d["category_id"], d["name"], secrets.token_hex(8), desc, d["protocol"],
        d["price"], d["duration"], d["traffic"], d["devices"]
    )
    await state.clear()
    await message.answer("✅ محصول ساخته شد.", reply_markup=home_keyboard(True))


@router.callback_query(F.data.startswith("a_product:"))
async def a_product(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    pid = int(call.data.split(":")[1])
    p = await one(
        """SELECT p.*,COALESCE(COUNT(c.id) FILTER(WHERE c.status='AVAILABLE'),0) stock,
                  COALESCE(COUNT(c.id) FILTER(WHERE c.status='SOLD'),0) sold
           FROM products p LEFT JOIN configs c ON c.product_id=p.id
           WHERE p.id=$1 GROUP BY p.id""",
        pid
    )
    if not p:
        return await call.message.edit_text("❌ پیدا نشد.")
    await call.message.edit_text(
        f"<b>🛍 {esc(p['name'])}</b>\n\n"
        f"نوع: {esc(p['protocol'])}\n"
        f"قیمت: {money(p['price'])} تومان\n"
        f"اعتبار: {p['duration_days']} روز\n"
        f"🟢 موجود: {p['stock']}\n"
        f"🔴 فروخته: {p['sold']}\n"
        f"وضعیت: {'فعال' if p['is_active'] else 'غیرفعال'}",
        reply_markup=markup([
            [("➕ افزودن کانفیگ", f"a_add_cfg:{pid}")],
            [("📥 افزودن گروهی", f"a_bulk_cfg:{pid}")],
            [("🔄 فعال/غیرفعال", f"a_toggle:{pid}")],
            [("🗑 حذف/غیرفعال", f"a_disable:{pid}")],
            [("🔙 محصولات", "a_products")]
        ])
    )


@router.callback_query(F.data.startswith("a_toggle:"))
async def a_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    pid = int(call.data.split(":")[1])
    await execute("UPDATE products SET is_active=NOT is_active,updated_at=NOW() WHERE id=$1", pid)
    await admin_log(call.from_user.id, "TOGGLE_PRODUCT", "PRODUCT", str(pid))
    await call.answer("تغییر کرد.")
    await a_product(call)


@router.callback_query(F.data.startswith("a_disable:"))
async def a_disable(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    pid = int(call.data.split(":")[1])
    await execute("UPDATE products SET is_active=FALSE,updated_at=NOW() WHERE id=$1", pid)
    await admin_log(call.from_user.id, "DISABLE_PRODUCT", "PRODUCT", str(pid))
    await call.answer("محصول غیرفعال شد.")
    await a_products(call)


@router.callback_query(F.data.startswith("a_add_cfg:"))
async def a_add_cfg(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await state.update_data(product_id=int(call.data.split(":")[1]))
    await state.set_state(AddConfig.text)
    await call.answer()
    await call.message.answer("کانفیگ را در یک پیام ارسال کن:")


@router.message(AddConfig.text)
async def add_config(message: Message, state: FSMContext):
    d = await state.get_data()
    text = (message.text or "").strip()
    if not text:
        return await message.answer("❌ کانفیگ خالی است.")
    await execute("INSERT INTO configs(product_id,config_text) VALUES($1,$2)", d["product_id"], text)
    await state.clear()
    await message.answer("✅ کانفیگ اضافه شد.", reply_markup=home_keyboard(True))


@router.callback_query(F.data.startswith("a_bulk_cfg:"))
async def a_bulk_cfg(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await state.update_data(product_id=int(call.data.split(":")[1]))
    await state.set_state(BulkConfig.text)
    await call.answer()
    await call.message.answer("هر کانفیگ را در یک خط جداگانه بفرست:")


@router.message(BulkConfig.text)
async def bulk_config(message: Message, state: FSMContext):
    d = await state.get_data()
    items = [x.strip() for x in (message.text or "").splitlines() if x.strip()]
    if not items:
        return await message.answer("❌ چیزی دریافت نشد.")
    async with pool.acquire() as con:
        async with con.transaction():
            await con.executemany(
                "INSERT INTO configs(product_id,config_text) VALUES($1,$2)",
                [(d["product_id"], x) for x in items]
            )
    await state.clear()
    await message.answer(f"✅ {len(items)} کانفیگ اضافه شد.", reply_markup=home_keyboard(True))


@router.callback_query(F.data == "a_configs")
async def a_configs(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    rs = await rows(
        """SELECT p.name,
           COUNT(c.id) FILTER(WHERE c.status='AVAILABLE') available,
           COUNT(c.id) FILTER(WHERE c.status='RESERVED') reserved,
           COUNT(c.id) FILTER(WHERE c.status='SOLD') sold
           FROM products p LEFT JOIN configs c ON c.product_id=p.id
           GROUP BY p.id ORDER BY p.id DESC"""
    )
    text = "<b>🔐 موجودی کانفیگ‌ها</b>\n\n"
    for r in rs:
        text += f"• {esc(r['name'])}\n🟢 {r['available']} | 🟡 {r['reserved']} | 🔴 {r['sold']}\n\n"
    await call.message.edit_text(text, reply_markup=back("admin"))


# ========================= ADMIN LISTS =========================

@router.callback_query(F.data == "a_orders")
async def a_orders(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    rs = await rows(
        """SELECT o.order_number,o.status,o.payable_amount,u.telegram_id,p.name
           FROM orders o JOIN users u ON u.id=o.user_id
           JOIN order_items oi ON oi.order_id=o.id JOIN products p ON p.id=oi.product_id
           ORDER BY o.created_at DESC LIMIT 25"""
    )
    text = "<b>🧾 سفارش‌ها</b>\n\n"
    for r in rs:
        text += f"<code>{r['order_number']}</code> | {esc(r['name'])}\n{money(r['payable_amount'])} | {r['status']} | <code>{r['telegram_id']}</code>\n\n"
    await call.message.edit_text(text, reply_markup=back("admin"))


@router.callback_query(F.data == "a_payments")
async def a_payments(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    rs = await rows(
        """SELECT pay.status,pay.method,pay.amount,o.order_number,u.telegram_id
           FROM payments pay JOIN orders o ON o.id=pay.order_id
           JOIN users u ON u.id=pay.user_id
           ORDER BY pay.created_at DESC LIMIT 25"""
    )
    text = "<b>💳 پرداخت‌ها</b>\n\n"
    for r in rs:
        text += f"<code>{r['order_number']}</code> | {r['method']} | {money(r['amount'])} | {r['status']}\n"
    await call.message.edit_text(text, reply_markup=back("admin"))


@router.callback_query(F.data == "a_users")
async def a_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    rs = await rows(
        """SELECT u.telegram_id,u.username,u.first_name,COALESCE(w.balance,0) balance
           FROM users u LEFT JOIN wallets w ON w.user_id=u.id
           ORDER BY u.created_at DESC LIMIT 30"""
    )
    text = "<b>👥 کاربران اخیر</b>\n\n"
    for r in rs:
        text += f"<code>{r['telegram_id']}</code> @{esc(r['username'] or '-')}\n💰 {money(r['balance'])}\n"
    await call.message.edit_text(text, reply_markup=back("admin"))


@router.callback_query(F.data == "a_stats")
async def a_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    r = await one(
        """SELECT
        (SELECT COUNT(*) FROM users) users,
        (SELECT COUNT(*) FROM products) products,
        (SELECT COUNT(*) FROM configs) configs,
        (SELECT COUNT(*) FROM configs WHERE status='AVAILABLE') available,
        (SELECT COUNT(*) FROM configs WHERE status='SOLD') sold,
        (SELECT COUNT(*) FROM orders WHERE status='COMPLETED') completed,
        (SELECT COALESCE(SUM(payable_amount),0) FROM orders WHERE status='COMPLETED') revenue"""
    )
    await call.message.edit_text(
        "<b>📊 گزارش‌ها</b>\n\n"
        f"👥 کاربران: {r['users']}\n"
        f"🛍 محصولات: {r['products']}\n"
        f"🔐 کانفیگ کل: {r['configs']}\n"
        f"🟢 موجود: {r['available']}\n"
        f"🔴 فروخته: {r['sold']}\n"
        f"🎉 فروش موفق: {r['completed']}\n"
        f"💰 درآمد: {money(r['revenue'])} تومان",
        reply_markup=back("admin")
    )


@router.callback_query(F.data == "a_settings")
async def a_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    await call.message.edit_text(
        "<b>⚙️ تنظیمات</b>\n\n"
        f"🏪 نام: {esc(SHOP_NAME)}\n"
        f"💳 کارت: <code>{esc(CARD_NUMBER)}</code>\n"
        f"👤 صاحب کارت: {esc(CARD_OWNER)}\n"
        f"🌐 درگاه: {'فعال' if PAYMENT_GATEWAY_URL else 'غیرفعال'}\n"
        f"🆘 پشتیبانی: @{esc(SUPPORT_USERNAME or '-')}\n"
        f"🎁 پاداش دعوت: {money(REFERRAL_REWARD)} تومان",
        reply_markup=back("admin")
    )


@router.callback_query(F.data == "a_referrals")
async def a_referrals(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    r = await one(
        """SELECT COUNT(*) total,
           COUNT(*) FILTER(WHERE status='COMPLETED') completed,
           COALESCE(SUM(reward_amount) FILTER(WHERE status='COMPLETED'),0) rewards
           FROM referrals"""
    )
    await call.message.edit_text(
        "<b>🎁 دعوت‌ها</b>\n\n"
        f"کل: {r['total']}\nموفق: {r['completed']}\nپاداش: {money(r['rewards'])} تومان",
        reply_markup=back("admin")
    )


@router.callback_query(F.data == "a_tickets")
async def a_tickets(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    rs = await rows(
        """SELECT t.ticket_number,t.subject,t.status,u.telegram_id
           FROM tickets t JOIN users u ON u.id=t.user_id
           WHERE t.status<>'CLOSED' ORDER BY t.created_at DESC LIMIT 30"""
    )
    text = "<b>🆘 تیکت‌های باز</b>\n\n"
    for r in rs:
        text += f"<code>{r['ticket_number']}</code> — {esc(r['subject'])} — <code>{r['telegram_id']}</code>\n"
    await call.message.edit_text(text or "تیکت بازی نیست.", reply_markup=back("admin"))


@router.callback_query(F.data == "a_wallet")
async def a_wallet(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    total = await value("SELECT COALESCE(SUM(balance),0) FROM wallets")
    await call.message.edit_text(
        "<b>💰 کیف پول</b>\n\n"
        f"مجموع موجودی کاربران: <b>{money(total)} تومان</b>\n\n"
        "افزایش موجودی باید پس از تأیید پرداخت ثبت شود.",
        reply_markup=back("admin")
    )


# ========================= DISCOUNTS ADMIN =========================

@router.callback_query(F.data == "a_discounts")
async def a_discounts(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    ds = await rows("SELECT * FROM discounts ORDER BY created_at DESC LIMIT 30")
    text = "<b>🎟 کدهای تخفیف</b>\n\n"
    for d in ds:
        text += f"• <code>{esc(d['code'])}</code> — {d['type']} {d['value']} — {'فعال' if d['is_active'] else 'غیرفعال'}\n"
    await call.message.edit_text(
        text,
        reply_markup=markup([
            [("➕ ساخت کد", "a_add_discount")],
            [("🔙 پنل", "admin")]
        ])
    )


@router.callback_query(F.data == "a_add_discount")
async def add_discount_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    await state.set_state(AddDiscount.code)
    await call.message.answer("کد تخفیف:")


@router.message(AddDiscount.code)
async def discount_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    if await value("SELECT 1 FROM discounts WHERE code=$1", code):
        return await message.answer("❌ این کد وجود دارد.")
    await state.update_data(code=code)
    await state.set_state(AddDiscount.type)
    await message.answer("نوع: PERCENT یا FIXED")


@router.message(AddDiscount.type)
async def discount_type(message: Message, state: FSMContext):
    t = message.text.strip().upper()
    if t not in ("PERCENT", "FIXED"):
        return await message.answer("فقط PERCENT یا FIXED")
    await state.update_data(type=t)
    await state.set_state(AddDiscount.value)
    await message.answer("مقدار:")


@router.message(AddDiscount.value)
async def discount_value(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(value=n)
    await state.set_state(AddDiscount.minimum)
    await message.answer("حداقل مبلغ سفارش؛ 0 یعنی بدون حداقل:")


@router.message(AddDiscount.minimum)
async def discount_minimum(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(minimum=n)
    await state.set_state(AddDiscount.limit)
    await message.answer("سقف استفاده کل؛ 0 یعنی نامحدود:")


@router.message(AddDiscount.limit)
async def discount_limit(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0: raise ValueError
    except Exception:
        return await message.answer("❌ عدد نامعتبر.")
    await state.update_data(limit=None if n == 0 else n)
    await state.set_state(AddDiscount.expires)
    await message.answer("تاریخ انقضا YYYY-MM-DD یا - :")


@router.message(AddDiscount.expires)
async def discount_expires(message: Message, state: FSMContext):
    d = await state.get_data()
    exp = None
    if message.text.strip() != "-":
        try:
            exp = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except Exception:
            return await message.answer("❌ تاریخ نامعتبر.")
    await execute(
        """INSERT INTO discounts(code,type,value,minimum_order_amount,usage_limit,expires_at)
           VALUES($1,$2,$3,$4,$5,$6)""",
        d["code"], d["type"], d["value"], d["minimum"], d["limit"], exp
    )
    await state.clear()
    await message.answer("✅ کد تخفیف ساخته شد.", reply_markup=home_keyboard(True))


# ========================= BROADCAST =========================

@router.callback_query(F.data == "a_broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔", show_alert=True)
    await call.answer()
    await state.set_state(Broadcast.message)
    await call.message.answer("متن پیام همگانی را بفرست:")


@router.message(Broadcast.message)
async def broadcast_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    users = await rows("SELECT telegram_id FROM users WHERE is_blocked=FALSE")
    bid = await value(
        "INSERT INTO broadcasts(admin_id,message,total_users) VALUES($1,$2,$3) RETURNING id",
        message.from_user.id, message.text, len(users)
    )
    sent = failed = 0
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], message.text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    await execute(
        """UPDATE broadcasts SET status='COMPLETED',sent_count=$1,
           failed_count=$2,completed_at=NOW() WHERE id=$3""",
        sent, failed, bid
    )
    await message.answer(
        f"📢 تمام شد.\n✅ {sent}\n❌ {failed}",
        reply_markup=home_keyboard(True)
    )


# ========================= BACKGROUND =========================

async def cleanup():
    while True:
        try:
            await execute(
                """UPDATE orders SET status='EXPIRED',updated_at=NOW()
                   WHERE status IN ('PENDING_PAYMENT','AWAITING_RECEIPT')
                   AND expires_at IS NOT NULL AND expires_at<NOW()"""
            )
            await execute(
                """UPDATE configs SET status='AVAILABLE',
                   reserved_by_order_id=NULL,reserved_until=NULL
                   WHERE status='RESERVED' AND reserved_until IS NOT NULL
                   AND reserved_until<NOW()"""
            )
        except Exception:
            log.exception("Cleanup error")
        await asyncio.sleep(300)


# ========================= RENDER WEBHOOK =========================

async def health(request):
    return web.Response(text="OK")


async def webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("Webhook update error")
        return web.Response(text="ERROR", status=200)


async def main():
    await db_init()
    log.info("PostgreSQL connected; schema ready.")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on %s", PORT)

    public_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if public_url:
        url = f"{public_url}/telegram/webhook"
        await bot.set_webhook(url, allowed_updates=dp.resolve_used_update_types())
        log.info("Telegram webhook set: %s", url)
    else:
        log.warning("RENDER_EXTERNAL_URL is missing.")

    asyncio.create_task(cleanup())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
