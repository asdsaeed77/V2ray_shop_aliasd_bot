import os
import asyncio
import sqlite3
import logging
import secrets
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Update
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============================================================
# V2RAY SHOP BOT - SINGLE FILE
# Render + GitHub friendly
#
# REQUIRED ENV:
# BOT_TOKEN
# ADMIN_IDS          e.g. 123456789,987654321
#
# OPTIONAL ENV:
# CARD_NUMBER
# CARD_OWNER
# PAYMENT_GATEWAY_URL
# SUPPORT_USERNAME
# DB_PATH
# PORT
#
# IMPORTANT:
# Render Free has an ephemeral filesystem. SQLite data can be
# lost after a restart/redeploy/spin-down. This version is
# intentionally dependency-light for a first free deployment.
# For production, migrate the database to PostgreSQL.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
CARD_NUMBER = os.getenv("CARD_NUMBER", "0000-0000-0000-0000").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "نام صاحب کارت").strip()
PAYMENT_GATEWAY_URL = os.getenv("PAYMENT_GATEWAY_URL", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "shop.db")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is not set")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("v2ray-shop")

# ---------------- DATABASE ----------------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'VLESS',
    description TEXT DEFAULT '',
    price INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    config TEXT NOT NULL,
    sold INTEGER NOT NULL DEFAULT 0,
    order_id TEXT,
    sold_at TEXT,
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    config_id INTEGER,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_receipt',
    receipt_file_id TEXT,
    receipt_type TEXT,
    admin_note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(id),
    FOREIGN KEY(config_id) REFERENCES configs(id)
);
""")
db.commit()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_exec(sql, params=(), commit=False):
    cur = db.execute(sql, params)
    if commit:
        db.commit()
    return cur


def money(n):
    return f"{int(n):,}"


def is_admin(uid):
    return uid in ADMIN_IDS


# ---------------- KEYBOARDS ----------------

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛍 محصولات", callback_data="products"),
            InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="myorders")
        ],
        [
            InlineKeyboardButton(text="💎 راهنما", callback_data="guide"),
            InlineKeyboardButton(text="🆘 پشتیبانی", callback_data="support")
        ],
    ])


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ افزودن محصول", callback_data="a_add_product"),
            InlineKeyboardButton(text="📝 ویرایش محصول", callback_data="a_edit_products")
        ],
        [
            InlineKeyboardButton(text="🗑 حذف محصول", callback_data="a_delete_products"),
            InlineKeyboardButton(text="➕ افزودن کانفیگ", callback_data="a_add_config")
        ],
        [
            InlineKeyboardButton(text="📦 موجودی", callback_data="a_inventory"),
            InlineKeyboardButton(text="🧾 سفارش‌های در انتظار", callback_data="a_pending")
        ],
        [
            InlineKeyboardButton(text="📊 آمار", callback_data="a_stats"),
            InlineKeyboardButton(text="⚙️ تنظیمات", callback_data="a_settings")
        ],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]
    ])


# ---------------- FSM ----------------

class AddProduct(StatesGroup):
    name = State()
    kind = State()
    price = State()
    description = State()


class AddConfig(StatesGroup):
    product = State()
    config = State()


class EditProduct(StatesGroup):
    product = State()
    field = State()
    value = State()


# ---------------- BOT ----------------

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


async def send_home(target):
    text = (
        "<b>✨ V2RAY SHOP</b>\n\n"
        "فروشگاه اختصاصی کانفیگ‌های VLESS / Trojan و سایر سرویس‌ها\n"
        "انتخاب کن، پرداخت کن و بعد از تأیید ادمین کانفیگ را دریافت کن.\n\n"
        "⚡ تحویل امن\n"
        "🔒 هر کانفیگ فقط یک‌بار فروخته می‌شود\n"
        "🧾 تأیید دستی پرداخت"
    )
    await target.answer(text, reply_markup=main_kb())


@router.message(CommandStart())
async def start(message: Message):
    db_exec(
        """
        INSERT INTO users(user_id,username,first_name,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (
            message.from_user.id,
            message.from_user.username or "",
            message.from_user.first_name or "",
            now()
        ),
        True
    )
    await send_home(message)


@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    await message.answer(
        "<b>🛠 پنل مدیریت</b>\nاز گزینه‌های زیر استفاده کنید:",
        reply_markup=admin_kb()
    )


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "<b>✨ V2RAY SHOP</b>\n\nبه فروشگاه خوش آمدید.",
        reply_markup=main_kb()
    )


@router.callback_query(F.data == "products")
async def products(call: CallbackQuery):
    await call.answer()
    rows = db_exec(
        "SELECT * FROM products WHERE active=1 ORDER BY id DESC"
    ).fetchall()

    if not rows:
        return await call.message.edit_text(
            "📭 فعلاً محصولی برای فروش موجود نیست.",
            reply_markup=back_kb()
        )

    buttons = []
    for p in rows:
        stock = db_exec(
            "SELECT COUNT(*) c FROM configs WHERE product_id=? AND sold=0",
            (p["id"],)
        ).fetchone()["c"]

        buttons.append([
            InlineKeyboardButton(
                text=f"🔹 {p['name']} | {money(p['price'])} تومان | موجودی: {stock}",
                callback_data=f"product:{p['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")
    ])

    await call.message.edit_text(
        "<b>🛍 محصولات</b>\n\nمحصول موردنظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("product:"))
async def product_detail(call: CallbackQuery):
    await call.answer()
    pid = int(call.data.split(":")[1])

    p = db_exec(
        "SELECT * FROM products WHERE id=? AND active=1",
        (pid,)
    ).fetchone()

    if not p:
        return await call.message.edit_text(
            "❌ محصول پیدا نشد.",
            reply_markup=back_kb()
        )

    stock = db_exec(
        "SELECT COUNT(*) c FROM configs WHERE product_id=? AND sold=0",
        (pid,)
    ).fetchone()["c"]

    text = (
        f"<b>{p['name']}</b>\n"
        f"نوع: <code>{p['kind']}</code>\n"
        f"قیمت: <b>{money(p['price'])} تومان</b>\n"
        f"موجودی: {stock}\n\n"
        f"{p['description'] or 'توضیحی ثبت نشده است.'}"
    )

    kb = []
    if stock:
        kb.append([
            InlineKeyboardButton(
                text="💳 خرید این محصول",
                callback_data=f"buy:{pid}"
            )
        ])
    else:
        text += "\n\n⛔ این محصول فعلاً ناموجود است."

    kb.append([
        InlineKeyboardButton(text="🔙 محصولات", callback_data="products")
    ])

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery):
    await call.answer()

    pid = int(call.data.split(":")[1])
    p = db_exec(
        "SELECT * FROM products WHERE id=? AND active=1",
        (pid,)
    ).fetchone()

    stock = db_exec(
        "SELECT COUNT(*) c FROM configs WHERE product_id=? AND sold=0",
        (pid,)
    ).fetchone()["c"]

    if not p or stock <= 0:
        return await call.message.edit_text(
            "⛔ این محصول موجود نیست.",
            reply_markup=back_kb()
        )

    order_id = secrets.token_hex(5).upper()

    db_exec(
        """
        INSERT INTO orders(
            id,user_id,product_id,amount,status,created_at,updated_at
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            order_id,
            call.from_user.id,
            pid,
            p["price"],
            "awaiting_receipt",
            now(),
            now()
        ),
        True
    )

    gateway = ""
    if PAYMENT_GATEWAY_URL:
        gateway = (
            f'\n\n🌐 <a href="{PAYMENT_GATEWAY_URL}">'
            "پرداخت آنلاین</a>"
        )

    text = (
        f"🧾 <b>سفارش {order_id}</b>\n\n"
        f"محصول: {p['name']}\n"
        f"مبلغ: <b>{money(p['price'])} تومان</b>\n\n"
        f"💳 <b>پرداخت کارت‌به‌کارت</b>\n"
        f"شماره کارت:\n<code>{CARD_NUMBER}</code>\n"
        f"به نام: <b>{CARD_OWNER}</b>"
        f"{gateway}\n\n"
        "پس از پرداخت، رسید را همین‌جا ارسال کن.\n"
        "⚠️ تا تأیید ادمین، کانفیگ تحویل نمی‌شود."
    )

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📎 ارسال رسید",
                    callback_data=f"receipt:{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو سفارش",
                    callback_data=f"cancel:{order_id}"
                )
            ]
        ])
    )


@router.callback_query(F.data.startswith("receipt:"))
async def receipt_hint(call: CallbackQuery):
    await call.answer(
        "رسید را به صورت عکس یا فایل در همین چت ارسال کنید.",
        show_alert=True
    )


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(call: CallbackQuery):
    oid = call.data.split(":")[1]

    row = db_exec(
        "SELECT * FROM orders WHERE id=? AND user_id=?",
        (oid, call.from_user.id)
    ).fetchone()

    if not row:
        return await call.answer(
            "سفارش پیدا نشد.",
            show_alert=True
        )

    if row["status"] not in ("awaiting_receipt", "pending_admin"):
        return await call.answer(
            "این سفارش قابل لغو نیست.",
            show_alert=True
        )

    db_exec(
        "UPDATE orders SET status='cancelled',updated_at=? WHERE id=?",
        (now(), oid),
        True
    )

    await call.answer("سفارش لغو شد.")
    await call.message.edit_text(
        "❌ سفارش لغو شد.",
        reply_markup=main_kb()
    )


async def forward_receipt(message: Message, oid: str):
    row = db_exec(
        """
        SELECT o.*,p.name
        FROM orders o
        JOIN products p ON p.id=o.product_id
        WHERE o.id=?
        """,
        (oid,)
    ).fetchone()

    if not row:
        return

    user = message.from_user
    caption = (
        f"🧾 <b>رسید جدید</b>\n\n"
        f"Order: <code>{oid}</code>\n"
        f"محصول: {row['name']}\n"
        f"مبلغ: <b>{money(row['amount'])} تومان</b>\n"
        f"کاربر: <code>{user.id}</code>\n"
        f"Username: @{user.username or '-'}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ تأیید و تحویل",
            callback_data=f"approve:{oid}"
        ),
        InlineKeyboardButton(
            text="❌ رد پرداخت",
            callback_data=f"reject:{oid}"
        )
    ]])

    fid = ""
    rtype = "text"

    for admin in ADMIN_IDS:
        try:
            if message.photo:
                await bot.send_photo(
                    admin,
                    message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=kb
                )
                rtype = "photo"
                fid = message.photo[-1].file_id

            elif message.document:
                await bot.send_document(
                    admin,
                    message.document.file_id,
                    caption=caption,
                    reply_markup=kb
                )
                rtype = "document"
                fid = message.document.file_id

            else:
                await bot.send_message(
                    admin,
                    caption,
                    reply_markup=kb
                )

        except Exception as e:
            log.exception("Admin notification failed: %s", e)

    db_exec(
        """
        UPDATE orders
        SET status='pending_admin',
            receipt_file_id=?,
            receipt_type=?,
            updated_at=?
        WHERE id=?
        """,
        (fid, rtype, now(), oid),
        True
    )


@router.message(F.photo)
async def photo_receipt(message: Message):
    row = db_exec(
        """
        SELECT id
        FROM orders
        WHERE user_id=?
          AND status='awaiting_receipt'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (message.from_user.id,)
    ).fetchone()

    if not row:
        return await message.answer(
            "❗ سفارش فعالی برای دریافت رسید پیدا نشد."
        )

    await forward_receipt(message, row["id"])
    await message.answer(
        "📨 رسید دریافت شد و برای ادمین ارسال شد. منتظر تأیید باشید."
    )


@router.message(F.document)
async def document_receipt(message: Message):
    row = db_exec(
        """
        SELECT id
        FROM orders
        WHERE user_id=?
          AND status='awaiting_receipt'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (message.from_user.id,)
    ).fetchone()

    if not row:
        return await message.answer(
            "❗ سفارش فعالی برای دریافت رسید پیدا نشد."
        )

    await forward_receipt(message, row["id"])
    await message.answer(
        "📨 رسید دریافت شد و برای ادمین ارسال شد. منتظر تأیید باشید."
    )


@router.callback_query(F.data.startswith("approve:"))
async def approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔",
            show_alert=True
        )

    oid = call.data.split(":")[1]

    row = db_exec(
        """
        SELECT o.*,p.name
        FROM orders o
        JOIN products p ON p.id=o.product_id
        WHERE o.id=?
        """,
        (oid,)
    ).fetchone()

    if not row or row["status"] != "pending_admin":
        return await call.answer(
            "این سفارش دیگر قابل تأیید نیست.",
            show_alert=True
        )

    # Select one unsold config and atomically mark it sold.
    cfg = db_exec(
        """
        SELECT *
        FROM configs
        WHERE product_id=?
          AND sold=0
        ORDER BY id
        LIMIT 1
        """,
        (row["product_id"],)
    ).fetchone()

    if not cfg:
        return await call.answer(
            "⛔ کانفیگ موجود برای این محصول نداریم.",
            show_alert=True
        )

    cur = db_exec(
        """
        UPDATE configs
        SET sold=1,order_id=?,sold_at=?
        WHERE id=? AND sold=0
        """,
        (oid, now(), cfg["id"])
    )

    if cur.rowcount != 1:
        db.rollback()
        return await call.answer(
            "کانفیگ همزمان توسط سفارش دیگری گرفته شد.",
            show_alert=True
        )

    db_exec(
        """
        UPDATE orders
        SET status='approved',
            config_id=?,
            updated_at=?
        WHERE id=?
        """,
        (cfg["id"], now(), oid),
        True
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"🎉 <b>پرداخت تأیید شد!</b>\n\n"
            f"سفارش: <code>{oid}</code>\n"
            f"محصول: {row['name']}\n\n"
            f"🔐 <b>کانفیگ اختصاصی شما:</b>\n"
            f"<pre>{cfg['config']}</pre>\n\n"
            "این کانفیگ به عنوان فروخته‌شده ثبت شد."
        )
    except Exception:
        log.exception("Could not deliver config")

    await call.answer(
        "تأیید شد و کانفیگ تحویل داده شد."
    )
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("reject:"))
async def reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔",
            show_alert=True
        )

    oid = call.data.split(":")[1]

    row = db_exec(
        "SELECT * FROM orders WHERE id=?",
        (oid,)
    ).fetchone()

    if not row or row["status"] != "pending_admin":
        return await call.answer(
            "این سفارش قابل رد نیست.",
            show_alert=True
        )

    db_exec(
        """
        UPDATE orders
        SET status='rejected',updated_at=?
        WHERE id=?
        """,
        (now(), oid),
        True
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"❌ <b>سفارش {oid} تأیید نشد.</b>\n\n"
            "سفارش شما توسط ادمین مربوطه تأیید نشد.\n"
            "در صورت اشتباه، با پشتیبانی تماس بگیرید."
        )
    except Exception:
        pass

    await call.answer("سفارش رد شد.")
    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "myorders")
async def myorders(call: CallbackQuery):
    await call.answer()

    rows = db_exec(
        """
        SELECT o.*,p.name
        FROM orders o
        JOIN products p ON p.id=o.product_id
        WHERE o.user_id=?
        ORDER BY o.created_at DESC
        LIMIT 10
        """,
        (call.from_user.id,)
    ).fetchall()

    if not rows:
        return await call.message.edit_text(
            "📭 هنوز سفارشی ندارید.",
            reply_markup=back_kb()
        )

    status_map = {
        "awaiting_receipt": "⏳ منتظر رسید",
        "pending_admin": "🔎 در انتظار بررسی",
        "approved": "✅ تأیید شده",
        "rejected": "❌ رد شده",
        "cancelled": "🚫 لغو شده",
    }

    text = "<b>📦 سفارش‌های من</b>\n\n"

    for o in rows:
        text += (
            f"• <code>{o['id']}</code> — {o['name']} — "
            f"{status_map.get(o['status'], o['status'])}\n"
        )

    await call.message.edit_text(
        text,
        reply_markup=back_kb()
    )


@router.callback_query(F.data == "guide")
async def guide(call: CallbackQuery):
    await call.answer()

    await call.message.edit_text(
        "<b>💎 راهنمای خرید</b>\n\n"
        "1️⃣ محصول را انتخاب کن.\n"
        "2️⃣ مبلغ را کارت‌به‌کارت یا از درگاه پرداخت کن.\n"
        "3️⃣ رسید را به صورت عکس/فایل بفرست.\n"
        "4️⃣ ادمین پرداخت را بررسی می‌کند.\n"
        "5️⃣ بعد از تأیید، یک کانفیگ موجود و استفاده‌نشده به صورت خودکار تحویل می‌شود.\n\n"
        "هر کانفیگ فقط یک بار قابل فروش است.",
        reply_markup=back_kb()
    )


@router.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.answer()

    if SUPPORT_USERNAME:
        text = (
            "🆘 پشتیبانی:\n"
            f"<a href='https://t.me/{SUPPORT_USERNAME}'>"
            f"@{SUPPORT_USERNAME}</a>"
        )
    else:
        text = (
            "🆘 برای پشتیبانی، شناسه پشتیبانی را در متغیر "
            "SUPPORT_USERNAME در Render تنظیم کنید."
        )

    await call.message.edit_text(
        text,
        reply_markup=back_kb()
    )


# ---------------- ADMIN ----------------

@router.callback_query(F.data.startswith("a_"))
async def admin_actions(
    call: CallbackQuery,
    state: FSMContext
):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

    action = call.data
    await call.answer()

    if action == "a_add_product":
        await state.set_state(AddProduct.name)
        return await call.message.answer(
            "نام محصول را بفرست:"
        )

    if action == "a_add_config":
        products = db_exec(
            "SELECT * FROM products WHERE active=1 ORDER BY id"
        ).fetchall()

        if not products:
            return await call.message.answer(
                "ابتدا یک محصول بسازید."
            )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=p["name"],
                    callback_data=f"pickcfg:{p['id']}"
                )
            ]
            for p in products
        ])

        await state.set_state(AddConfig.product)

        return await call.message.answer(
            "محصول مقصد را انتخاب کنید:",
            reply_markup=kb
        )

    if action == "a_edit_products":
        return await admin_product_list(
            call,
            "edit"
        )

    if action == "a_delete_products":
        return await admin_product_list(
            call,
            "delete"
        )

    if action == "a_inventory":
        rows = db_exec(
            """
            SELECT
                p.name,
                COUNT(c.id) total,
                SUM(CASE WHEN c.sold=0 THEN 1 ELSE 0 END) available,
                SUM(CASE WHEN c.sold=1 THEN 1 ELSE 0 END) sold
            FROM products p
            LEFT JOIN configs c ON c.product_id=p.id
            GROUP BY p.id
            ORDER BY p.id
            """
        ).fetchall()

        text = "<b>📦 موجودی</b>\n\n"

        for r in rows:
            text += (
                f"• {r['name']}: "
                f"کل {r['total'] or 0} | "
                f"موجود {r['available'] or 0} | "
                f"فروخته {r['sold'] or 0}\n"
            )

        return await call.message.answer(
            text or "موجودی خالی است."
        )

    if action == "a_pending":
        rows = db_exec(
            """
            SELECT
                o.id,
                o.amount,
                o.created_at,
                p.name,
                u.user_id,
                u.username
            FROM orders o
            JOIN products p ON p.id=o.product_id
            JOIN users u ON u.user_id=o.user_id
            WHERE o.status='pending_admin'
            ORDER BY o.created_at DESC
            """
        ).fetchall()

        if not rows:
            return await call.message.answer(
                "✅ سفارشی در انتظار بررسی نیست."
            )

        for r in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ تأیید",
                    callback_data=f"approve:{r['id']}"
                ),
                InlineKeyboardButton(
                    text="❌ رد",
                    callback_data=f"reject:{r['id']}"
                )
            ]])

            await call.message.answer(
                f"🧾 <code>{r['id']}</code>\n"
                f"{r['name']}\n"
                f"{money(r['amount'])} تومان\n"
                f"کاربر: <code>{r['user_id']}</code> "
                f"@{r['username'] or '-'}",
                reply_markup=kb
            )

        return

    if action == "a_stats":
        users = db_exec(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        orders = db_exec(
            "SELECT COUNT(*) c FROM orders"
        ).fetchone()["c"]

        approved = db_exec(
            "SELECT COUNT(*) c FROM orders WHERE status='approved'"
        ).fetchone()["c"]

        revenue = db_exec(
            """
            SELECT COALESCE(SUM(amount),0) s
            FROM orders
            WHERE status='approved'
            """
        ).fetchone()["s"]

        configs = db_exec(
            "SELECT COUNT(*) c FROM configs"
        ).fetchone()["c"]

        sold = db_exec(
            "SELECT COUNT(*) c FROM configs WHERE sold=1"
        ).fetchone()["c"]

        return await call.message.answer(
            f"<b>📊 آمار فروشگاه</b>\n\n"
            f"👥 کاربران: {users}\n"
            f"🧾 سفارش‌ها: {orders}\n"
            f"✅ فروش موفق: {approved}\n"
            f"💰 درآمد ثبت‌شده: {money(revenue)} تومان\n"
            f"🔐 کانفیگ‌ها: {configs}\n"
            f"📤 فروخته‌شده: {sold}"
        )

    if action == "a_settings":
        return await call.message.answer(
            "<b>⚙️ تنظیمات</b>\n\n"
            f"شماره کارت: <code>{CARD_NUMBER}</code>\n"
            f"صاحب کارت: {CARD_OWNER}\n"
            f"درگاه: {'فعال' if PAYMENT_GATEWAY_URL else 'غیرفعال'}\n"
            f"پشتیبانی: @{SUPPORT_USERNAME or '-'}\n\n"
            "برای تغییر این موارد، Environment Variables "
            "رندر را ویرایش و سرویس را redeploy کنید."
        )


async def admin_product_list(call, mode):
    rows = db_exec(
        "SELECT * FROM products ORDER BY id DESC"
    ).fetchall()

    if not rows:
        return await call.message.answer(
            "محصولی وجود ندارد."
        )

    prefix = "editpick:" if mode == "edit" else "delpick:"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=p["name"],
                callback_data=f"{prefix}{p['id']}"
            )
        ]
        for p in rows
    ])

    await call.message.answer(
        "محصول را انتخاب کنید:",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("pickcfg:"))
async def pick_config_product(
    call: CallbackQuery,
    state: FSMContext
):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔",
            show_alert=True
        )

    pid = int(call.data.split(":")[1])

    await state.update_data(product_id=pid)
    await state.set_state(AddConfig.config)
    await call.answer()

    await call.message.answer(
        "کانفیگ را دقیقاً در یک پیام ارسال کنید.\n"
        "مثال: vless://... یا trojan://...\n\n"
        "هر کانفیگ فقط یک بار فروخته خواهد شد."
    )


@router.message(AddProduct.name)
async def add_product_name(
    message: Message,
    state: FSMContext
):
    await state.update_data(
        name=message.text.strip()
    )
    await state.set_state(AddProduct.kind)

    await message.answer(
        "نوع را بفرست (مثلاً VLESS / Trojan / VMess):"
    )


@router.message(AddProduct.kind)
async def add_product_kind(
    message: Message,
    state: FSMContext
):
    await state.update_data(
        kind=message.text.strip()
    )
    await state.set_state(AddProduct.price)

    await message.answer(
        "قیمت به تومان را فقط به عدد بفرست:"
    )


@router.message(AddProduct.price)
async def add_product_price(
    message: Message,
    state: FSMContext
):
    try:
        price = int(
            message.text
            .replace(",", "")
            .replace("٬", "")
            .strip()
        )
        if price < 0:
            raise ValueError
    except (ValueError, AttributeError):
        return await message.answer(
            "❌ قیمت نامعتبر است. فقط عدد بفرست."
        )

    await state.update_data(price=price)
    await state.set_state(AddProduct.description)

    await message.answer(
        "توضیحات محصول را بفرست (یا - برای بدون توضیح):"
    )


@router.message(AddProduct.description)
async def add_product_description(
    message: Message,
    state: FSMContext
):
    data = await state.get_data()

    desc = (
        ""
        if message.text.strip() == "-"
        else message.text.strip()
    )

    db_exec(
        """
        INSERT INTO products(
            name,kind,description,price,created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            data["name"],
            data["kind"],
            desc,
            data["price"],
            now()
        ),
        True
    )

    await state.clear()

    await message.answer(
        "✅ محصول با موفقیت اضافه شد.",
        reply_markup=admin_kb()
    )


@router.message(AddConfig.config)
async def add_config(
    message: Message,
    state: FSMContext
):
    cfg = message.text.strip() if message.text else ""

    if not cfg:
        return await message.answer(
            "❌ متن کانفیگ خالی است."
        )

    data = await state.get_data()

    db_exec(
        "INSERT INTO configs(product_id,config) VALUES(?,?)",
        (data["product_id"], cfg),
        True
    )

    await state.clear()

    count = db_exec(
        """
        SELECT COUNT(*) c
        FROM configs
        WHERE product_id=? AND sold=0
        """,
        (data["product_id"],)
    ).fetchone()["c"]

    await message.answer(
        f"✅ کانفیگ اضافه شد. "
        f"موجودی فعلی این محصول: {count}",
        reply_markup=admin_kb()
    )


@router.callback_query(F.data.startswith("editpick:"))
async def edit_pick(
    call: CallbackQuery,
    state: FSMContext
):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔",
            show_alert=True
        )

    pid = int(call.data.split(":")[1])

    p = db_exec(
        "SELECT * FROM products WHERE id=?",
        (pid,)
    ).fetchone()

    if not p:
        return await call.answer(
            "پیدا نشد",
            show_alert=True
        )

    await state.update_data(
        product_id=pid
    )
    await state.set_state(
        EditProduct.field
    )
    await call.answer()

    await call.message.answer(
        f"ویرایش <b>{p['name']}</b>:\n"
        "یکی را بفرست:\n"
        "name = نام\n"
        "kind = نوع\n"
        "price = قیمت\n"
        "description = توضیح"
    )


@router.message(EditProduct.field)
async def edit_field(
    message: Message,
    state: FSMContext
):
    field = message.text.strip().lower()

    if field not in (
        "name",
        "kind",
        "price",
        "description"
    ):
        return await message.answer(
            "یکی از این‌ها را بفرست: "
            "name / kind / price / description"
        )

    await state.update_data(
        field=field
    )
    await state.set_state(
        EditProduct.value
    )

    await message.answer(
        "مقدار جدید را بفرست:"
    )


@router.message(EditProduct.value)
async def edit_value(
    message: Message,
    state: FSMContext
):
    data = await state.get_data()
    value = message.text.strip()

    if data["field"] == "price":
        try:
            value = int(
                value.replace(",", "")
                .replace("٬", "")
            )
        except ValueError:
            return await message.answer(
                "قیمت باید عدد باشد."
            )

    # Field is restricted by the state above, so this is safe.
    db_exec(
        f"UPDATE products SET {data['field']}=? WHERE id=?",
        (value, data["product_id"]),
        True
    )

    await state.clear()

    await message.answer(
        "✅ محصول ویرایش شد.",
        reply_markup=admin_kb()
    )


@router.callback_query(F.data.startswith("delpick:"))
async def delete_pick(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer(
            "⛔",
            show_alert=True
        )

    pid = int(call.data.split(":")[1])

    p = db_exec(
        "SELECT * FROM products WHERE id=?",
        (pid,)
    ).fetchone()

    if not p:
        return await call.answer(
            "پیدا نشد",
            show_alert=True
        )

    # Soft-delete so old orders remain valid.
    db_exec(
        "UPDATE products SET active=0 WHERE id=?",
        (pid,),
        True
    )

    await call.answer("حذف شد.")

    await call.message.edit_text(
        f"🗑 محصول «{p['name']}» غیرفعال شد.",
        reply_markup=admin_kb()
    )


# ---------------- RENDER HEALTH + TELEGRAM WEBHOOK ----------------

async def health(request):
    return web.Response(text="OK")


async def telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("Webhook update failed")
        # Return 200 so Telegram does not hammer a permanently bad update.
        return web.Response(text="ERROR", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )
    await site.start()

    log.info("HTTP server listening on %s", PORT)

    public_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

    if public_url:
        webhook_url = f"{public_url}/telegram/webhook"

        try:
            await bot.set_webhook(
                webhook_url,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False
            )
            log.info("Telegram webhook set: %s", webhook_url)
        except Exception:
            log.exception("Could not set Telegram webhook")
    else:
        log.warning(
            "RENDER_EXTERNAL_URL is missing. "
            "Webhook cannot be configured automatically."
        )

    return runner


async def main():
    await start_web_server()

    # Keep the process alive. Telegram sends updates to the webhook.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
