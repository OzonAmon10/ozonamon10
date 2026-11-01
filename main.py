import os
import sqlite3
import httpx
import contextvars
from functools import wraps
from datetime import datetime, date, timedelta
import uuid
import re
from pathlib import Path

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OWNER_TELEGRAM_ID = os.getenv("OWNER_TELEGRAM_ID", "").strip()
_current_db = contextvars.ContextVar("current_user_db", default=None)
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the hosting panel environment variables.")

# ВАЖНО: путь к базе оставлен таким же, как в предыдущей версии.
DATA_DIR = Path("/app/data")
if not DATA_DIR.exists():
    DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ozonamon.db"  # Старая база владельца — сохраняется без изменений.
USER_DB_DIR = DATA_DIR / "users"
USER_DB_DIR.mkdir(parents=True, exist_ok=True)


def user_database(func):
    """Каждый Telegram-пользователь работает со своей SQLite-базой."""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user = update.effective_user
        if user is None:
            return await func(update, context, *args, **kwargs)
        # Если ID владельца задан, ему сохраняем старую базу; остальным — отдельные.
        if OWNER_TELEGRAM_ID and str(user.id) == OWNER_TELEGRAM_ID:
            path = DB_PATH
        else:
            path = USER_DB_DIR / f"{user.id}.db"
        token = _current_db.set(path)
        try:
            init_db()
            return await func(update, context, *args, **kwargs)
        finally:
            _current_db.reset(token)
    return wrapped

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📦 Товары"), KeyboardButton("➕ Добавить товар")],
        [KeyboardButton("💰 Изменить себестоимость"), KeyboardButton("🗑 Удалить товар")],
        [KeyboardButton("🛒 Продажа"), KeyboardButton("↩️ Возврат")],
        [KeyboardButton("💸 Расход"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("📜 История"), KeyboardButton("🧹 Сбросить продажи")],
        [KeyboardButton("📅 Продажа за день"), KeyboardButton("📊 Отчёт за дату")],
        [KeyboardButton("🧮 Юнит-экономика")],
        [KeyboardButton("🔎 Поиск товара"), KeyboardButton("🤖 ИИ-помощник")],
        [KeyboardButton("⬅️ Назад"), KeyboardButton("🏠 Главное меню")],
    ],
    resize_keyboard=True
)

INPUT_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("⬅️ Назад"), KeyboardButton("🏠 Главное меню")],
        [KeyboardButton("❌ Отмена")]
    ],
    resize_keyboard=True
)

NAV_BUTTONS = [
    [InlineKeyboardButton("⬅️ Назад", callback_data="nav_back"),
     InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
]

def connect():
    db_path = _current_db.get() or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            unit_cost REAL NOT NULL,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            product_name TEXT,
            qty INTEGER NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            order_id TEXT
        )""")
        # Безопасная миграция старой базы: существующие операции не удаляются.
        columns = {row["name"] for row in c.execute("PRAGMA table_info(operations)")}
        if "order_id" not in columns:
            c.execute("ALTER TABLE operations ADD COLUMN order_id TEXT")

def now():
    return datetime.now().isoformat(timespec="seconds")

def money(value):
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"

def product_rows():
    with connect() as c:
        return c.execute("SELECT * FROM products ORDER BY name").fetchall()

def get_product(product_id):
    with connect() as c:
        return c.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()

def save_op(kind, product_name=None, qty=0, amount=0, note=None, order_id=None):
    with connect() as c:
        cur = c.execute(
            """INSERT INTO operations
            (kind, product_name, qty, amount, note, created_at, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kind, product_name, int(qty), float(amount), note, now(), order_id)
        )
        return cur.lastrowid

def product_keyboard(action):
    rows = product_rows()
    if not rows:
        return None
    buttons = [
        [InlineKeyboardButton(
            f"{r['name']} · {money(r['unit_cost'])}/шт.",
            callback_data=f"{action}:{r['id']}"
        )]
        for r in rows
    ]
    buttons.extend(NAV_BUTTONS)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)

def cancel_keyboard():
    return InlineKeyboardMarkup(
        NAV_BUTTONS + [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
    )

# --- Калькулятор юнит-экономики ---
UNIT_STEPS = [
    ("category", "категорию товара"),
    ("scheme", "схему FBO/FBS"),
    ("cost", "себестоимость товара"),
    ("volume", "габариты или литраж"),
    ("drr", "ДРР"),
    ("spp", "процент СПП"),
    ("tax_system", "систему налогообложения"),
    ("tax_rate", "ставку налога"),
    ("vat", "ставку НДС"),
    ("buyout", "процент выкупа"),
    ("price", "цену продажи на Ozon"),
    ("packing", "стоимость упаковки"),
    ("extra", "дополнительные расходы"),
]


def parse_volume(value):
    value = value.strip().lower().replace(',', '.')
    if 'x' in value or 'х' in value:
        parts = re.split(r'[xх]', value)
        if len(parts) != 3:
            raise ValueError
        a, b, c = [float(x.strip()) for x in parts]
        if min(a, b, c) <= 0:
            raise ValueError
        return a * b * c / 1_000_000  # мм³ -> литры
    value = value.replace('литра', '').replace('литров', '').replace('литр', '').replace('л', '').strip()
    volume = float(value)
    if volume <= 0 or volume > 100000:
        raise ValueError
    return volume


def unit_step_keyboard(field):
    rows = []
    if field == 'scheme':
        rows = [[InlineKeyboardButton('FBO', callback_data='unit_set:scheme:FBO'),
                 InlineKeyboardButton('FBS', callback_data='unit_set:scheme:FBS')]]
    elif field == 'tax_system':
        rows = [[InlineKeyboardButton(x, callback_data=f'unit_set:tax_system:{x}') for x in ('УСН', 'АУСН', 'ОСНО')],
                [InlineKeyboardButton('Ввести % вручную', callback_data='unit_manual_tax')]]
    elif field == 'tax_rate':
        rows = [[InlineKeyboardButton('УСН 6%', callback_data='unit_set:tax_rate:6'),
                 InlineKeyboardButton('УСН 15%', callback_data='unit_set:tax_rate:15')],
                [InlineKeyboardButton('Ввести % вручную', callback_data='unit_manual:tax_rate')]]
    elif field == 'vat':
        rows = [[InlineKeyboardButton(f'{x}%', callback_data=f'unit_set:vat:{x}') for x in (0, 5, 7)],
                [InlineKeyboardButton(f'{x}%', callback_data=f'unit_set:vat:{x}') for x in (20, 22)],
                [InlineKeyboardButton('Ввести % вручную', callback_data='unit_manual:vat')]]
    elif field == 'spp':
        rows = [[InlineKeyboardButton('0%', callback_data='unit_set:spp:0')]]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='unit_back')])
    rows.append([InlineKeyboardButton('🏠 Главное меню', callback_data='unit_menu')])
    return InlineKeyboardMarkup(rows)


async def unit_prompt(message, context):
    step = context.user_data.get('unit_step', 0)
    if step < 0 or step >= len(UNIT_STEPS):
        context.user_data['unit_step'] = 0
        step = 0
    field, _ = UNIT_STEPS[step]
    prompts = {
        'category': 'Шаг 1. Напишите название категории товара, как на Ozon.',
        'scheme': 'Шаг 2. Выберите схему:\nFBO — Ozon хранит и доставляет товар сам.\nFBS — товар хранится у вас, отправка со своего склада.',
        'cost': 'Шаг 3. Введите себестоимость товара в рублях.',
        'volume': 'Шаг 4. Введите габариты ДxШxВ в мм (например 250x140x80) или литраж вручную (например 1.3 л).',
        'drr': 'Шаг 5. Введите ДРР в процентах. Например 6.',
        'spp': 'Шаг 6. Укажите % СПП. Если СПП нет — нажмите 0.',
        'tax_system': 'Шаг 7. Выберите систему налогообложения: УСН / АУСН / ОСНО. Можно ввести ставку вручную.',
        'tax_rate': 'Шаг 7. Выберите ставку налога или введите процент вручную.',
        'vat': 'Шаг 8. Выберите ставку НДС или введите процент вручную.',
        'buyout': 'Шаг 9. Введите процент выкупа. Например 90.',
        'price': 'Шаг 10. Введите цену продажи на Ozon в рублях.',
        'packing': 'Шаг 11. Укажите стоимость упаковки в рублях. Если её нет — 0.',
        'extra': 'Шаг 12. Укажите дополнительные расходы на 1 единицу товара в рублях. Если их нет — 0.',
    }
    if field in ('scheme', 'tax_system', 'tax_rate', 'vat', 'spp'):
        await message.reply_text(prompts[field], reply_markup=unit_step_keyboard(field))
    else:
        await message.reply_text(prompts[field], reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('⬅️ Назад', callback_data='unit_back')],
            [InlineKeyboardButton('🏠 Главное меню', callback_data='unit_menu')]
        ]))


async def finish_unit_calc(message, context):
    d = context.user_data.get('unit_data', {})
    required = [x[0] for x in UNIT_STEPS]
    if any(k not in d for k in required):
        await message.reply_text('Не все данные заполнены. Начните расчёт заново.', reply_markup=MENU)
        context.user_data.clear()
        return
    # Базовые ставки можно переопределить в настройках хостинга.
    scheme = d['scheme']
    commission_rate = float(os.getenv(f'OZON_COMMISSION_{scheme}', '40'))
    acquiring_rate = float(os.getenv('OZON_ACQUIRING_RATE', '1.5'))
    logistics_per_liter = float(os.getenv('OZON_LOGISTICS_PER_LITER', '58.92'))
    price = float(d['price'])
    buyer_price = price * (1 - float(d['spp']) / 100)
    commission = price * commission_rate / 100
    acquiring = price * acquiring_rate / 100
    logistics = float(d['volume']) * logistics_per_liter
    drr = price * float(d['drr']) / 100
    tax = buyer_price * float(d['tax_rate']) / 100
    vat = buyer_price * float(d['vat']) / 100
    external = float(d['cost']) + float(d['packing']) + float(d['extra']) + tax + vat
    marketplace = commission + acquiring + logistics + drr
    margin = price - external - marketplace
    margin_pct = (margin / price * 100) if price else 0
    context.user_data['unit_result'] = dict(d)
    lines = [
        f"📦 Юнит-экономика Ozon ({scheme})",
        f"Товар: {d['category']}",
        f"Цена продажи: {money(price)}",
        f"СПП: {d['spp']:.2f}% | Цена покупателя: {money(buyer_price)}",
        f"Себестоимость: {money(d['cost'])}",
        f"Объём: {d['volume']:.3f} л | Выкуп: {d['buyout']:.2f}%",
        f"Налог: {d['tax_system']} {d['tax_rate']:.2f}% | НДС: {d['vat']:.2f}%",
        '', 'Расходы:',
        f"• Комиссия ({commission_rate:.2f}%): {money(commission)}",
        f"• Эквайринг ({acquiring_rate:.2f}%): {money(acquiring)}",
        f"• Логистика: {money(logistics)}",
        f"• ДРР ({d['drr']:.2f}%): {money(drr)}",
        f"• Упаковка: {money(d['packing'])}",
        f"• Доп. расходы: {money(d['extra'])}",
        f"• Налог от цены покупателя: {money(tax)}",
        f"• НДС (расчётно): {money(vat)}",
        '', 'Итоги:',
        f"Внешние расходы: {money(external)}",
        f"Расходы маркетплейса: {money(marketplace)}",
        f"Маржа на 1 ед.: {money(margin)}",
        f"% маржи от цены продажи: {margin_pct:.2f}%",
        '', '⚠️ Предварительный расчёт. Тариф комиссии и логистики зависят от категории, схемы и условий Ozon. Проверьте ставки в настройках и кабинете продавца.'
    ]
    fields = [x[0] for x in UNIT_STEPS]
    buttons = [[InlineKeyboardButton('Изменить: ' + UNIT_STEPS[i][1], callback_data='unit_edit:' + field)]
               for i, field in enumerate(fields)]
    buttons = [[InlineKeyboardButton('🔄 Новый расчёт', callback_data='unit_new')],
               [InlineKeyboardButton('🏠 В главное меню', callback_data='unit_menu')]] + buttons
    context.user_data['flow'] = None
    await message.reply_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(buttons))

def period_start(period):
    today = date.today()
    if period == "day":
        return today.isoformat()
    if period == "week":
        return (today - timedelta(days=6)).isoformat()
    if period == "month":
        return today.replace(day=1).isoformat()
    return "0001-01-01"

def stats_text(period):
    start = period_start(period)
    label = {
        "day": "сегодня",
        "week": "за последние 7 дней",
        "month": "с начала месяца",
        "all": "за всё время"
    }[period]
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM operations WHERE date(created_at) >= date(?) ORDER BY created_at",
            (start,)
        ).fetchall()

    sold_qty = sum(r["qty"] for r in rows if r["kind"] == "sale")
    returned_qty = sum(r["qty"] for r in rows if r["kind"] == "return")
    sold_cost = sum(r["amount"] for r in rows if r["kind"] == "sale")
    returned_cost = sum(r["amount"] for r in rows if r["kind"] == "return")
    expenses = sum(r["amount"] for r in rows if r["kind"] == "expense")

    return (
        f"📊 Статистика {label}\n\n"
        f"Продано: {sold_qty} шт.\n"
        f"Возвращено: {returned_qty} шт.\n"
        f"Себестоимость продаж: {money(sold_cost)}\n"
        f"Себестоимость возвратов: −{money(returned_cost)}\n"
        f"Расходы: {money(expenses)}\n\n"
        "Показатели считаются по сохранённым операциям. Это учёт себестоимости и расходов, "
        "а не выручка или бухгалтерская чистая прибыль."
    )

@user_database
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Добро пожаловать в OZONAMON 10!\n\n"
        "Теперь товары для продаж и возвратов выбираются кнопками. "
        "Себестоимость можно менять, а ошибочные операции — удалять.",
        reply_markup=MENU
    )

@user_database
async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Каталог пуст. Нажмите «➕ Добавить товар».", reply_markup=MENU)
        return
    lines = [f"• {r['name']} — {money(r['unit_cost'])}/шт." for r in rows]
    await update.message.reply_text("📦 Товары в каталоге:\n\n" + "\n".join(lines), reply_markup=MENU)

@user_database
async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["flow"] = "add_name"
    await update.message.reply_text("Введите название нового товара:", reply_markup=INPUT_MENU)

async def begin_product_action(query, context, action, title):
    keyboard = product_keyboard(action)
    if not keyboard:
        await query.message.reply_text("В каталоге пока нет товаров.", reply_markup=MENU)
        return
    await query.message.reply_text(title, reply_markup=keyboard)

@user_database
async def begin_sale(update, context):
    await update.message.reply_text(
        "🛒 Выберите товар для продажи:",
        reply_markup=product_keyboard("sale") or cancel_keyboard()
    )

@user_database
async def begin_return(update, context):
    await update.message.reply_text(
        "↩️ Выберите товар, который вернули:",
        reply_markup=product_keyboard("return") or cancel_keyboard()
    )

@user_database
async def begin_cost_change(update, context):
    await update.message.reply_text(
        "💰 Выберите товар, у которого нужно изменить себестоимость:",
        reply_markup=product_keyboard("cost") or cancel_keyboard()
    )

@user_database
async def begin_delete_product(update, context):
    await update.message.reply_text(
        "🗑 Выберите товар для удаления:",
        reply_markup=product_keyboard("delete_product") or cancel_keyboard()
    )

@user_database
async def begin_expense(update, context):
    context.user_data["flow"] = "expense_name"
    await update.message.reply_text(
        "Напишите название расхода (например, доставка или упаковка):",
        reply_markup=INPUT_MENU
    )

@user_database
async def begin_search(update, context):
    context.user_data["flow"] = "search"
    await update.message.reply_text("Введите название или часть названия товара:", reply_markup=INPUT_MENU)


@user_database
async def begin_ai_assistant(update, context):
    context.user_data["flow"] = "ai_question"
    await update.message.reply_text(
        "🤖 ИИ-помощник OZONAMON 10\n\n"
        "Напишите вопрос: например, как посчитать себестоимость, "
        "разобрать расходы или улучшить учёт продаж.\n\n"
        "ИИ-помощник требует OPENAI_API_KEY в настройках Bothost.",
        reply_markup=INPUT_MENU
    )


async def ask_ai(question, context):
    if not OPENAI_API_KEY:
        return (
            "ИИ-помощник пока не подключён.\n\n"
            "Добавьте переменную OPENAI_API_KEY в настройках приложения Bothost "
            "и укажите свой API-ключ OpenAI. Не отправляйте ключ в чат и не публикуйте его."
        )
    history = context.user_data.setdefault("ai_history", [])
    history.append({"role": "user", "content": question})
    history = history[-10:]
    payload = {
        "model": AI_MODEL,
        "input": [
            {"role": "system", "content": (
                "Ты — краткий и понятный помощник владельца небольшого магазина. "
                "Помогай с учётом товаров, себестоимостью, продажами, возвратами, "
                "расходами и работой с маркетплейсами. Отвечай по-русски, простыми словами. "
                "Не выдумывай данные магазина: если цифр не дали, попроси их."
            )},
            *history
        ],
        "max_output_tokens": 700
    }
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            response.raise_for_status()
            data = response.json()
        answer_parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    answer_parts.append(content.get("text", ""))
        answer = "\\n".join(part for part in answer_parts if part).strip()
        if not answer:
            answer = "Не получилось получить ответ. Попробуйте задать вопрос иначе."
        history.append({"role": "assistant", "content": answer})
        context.user_data["ai_history"] = history[-10:]
        return answer[:4000]
    except httpx.HTTPStatusError as exc:
        return f"Ошибка ИИ-сервиса ({exc.response.status_code}). Проверьте API-ключ и доступ к модели."
    except Exception:
        return "Не удалось связаться с ИИ-сервисом. Попробуйте позже."


@user_database
async def begin_batch_sale(update, context):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Сначала добавьте товары в каталог.", reply_markup=MENU)
        return
    context.user_data["batch_sale_items"] = []
    await send_batch_sale_picker(update.message, context)

async def send_batch_sale_picker(message, context):
    rows = product_rows()
    buttons = [
        [InlineKeyboardButton(f"{r['name']} · {money(r['unit_cost'])}/шт.",
                              callback_data=f"batch_sale:{r['id']}")]
        for r in rows
    ]
    buttons.append([InlineKeyboardButton("✅ Завершить и сохранить продажу", callback_data="batch_sale_done")])
    buttons.extend(NAV_BUTTONS)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    cart = context.user_data.get("batch_sale_items", [])
    summary = "Пока ничего не добавлено."
    if cart:
        summary = "Добавлено в дневную продажу:\n" + "\n".join(
            f"• {item['name']} — {item['qty']} шт. × {money(item['unit_cost'])} = {money(item['qty'] * item['unit_cost'])}"
            for item in cart
        )
    await message.reply_text(
        "📅 Продажа за день\n\nВыбирай товар, вводи количество. "
        "Можно добавить несколько товаров.\n\n" + summary,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@user_database
async def show_history(update, context):
    with connect() as c:
        rows = c.execute("SELECT * FROM operations ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        await update.message.reply_text("Операций пока нет.", reply_markup=MENU)
        return

    lines = ["📜 Последние операции (нажмите на операцию, чтобы удалить ошибочную):"]
    buttons = []
    for r in rows:
        dt = r["created_at"].replace("T", " ")
        if r["kind"] == "sale":
            desc = f"Продажа · {r['product_name']} · {r['qty']} шт. · {money(r['amount'])}"
        elif r["kind"] == "return":
            desc = f"Возврат · {r['product_name']} · {r['qty']} шт. · {money(r['amount'])}"
        else:
            desc = f"Расход · {r['note']} · {money(r['amount'])}"
        lines.append(f"\n#{r['id']} · {dt}\n{desc}")
        short = desc if len(desc) <= 48 else desc[:45] + "..."
        buttons.append([InlineKeyboardButton(f"🗑 #{r['id']} · {short}", callback_data=f"op_delete:{r['id']}")])

    buttons.extend(NAV_BUTTONS)
    buttons.append([InlineKeyboardButton("Закрыть", callback_data="cancel")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

@user_database
async def show_stats(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Сегодня", callback_data="stats:day"),
         InlineKeyboardButton("7 дней", callback_data="stats:week")],
        [InlineKeyboardButton("Этот месяц", callback_data="stats:month"),
         InlineKeyboardButton("Всё время", callback_data="stats:all")],
        *NAV_BUTTONS
    ])
    await update.message.reply_text(stats_text("day"), reply_markup=keyboard)

@user_database
async def begin_reset_sales(update, context):
    with connect() as c:
        count = c.execute("SELECT COUNT(*) FROM operations WHERE kind='sale'").fetchone()[0]
    if not count:
        await update.message.reply_text("Записанных продаж пока нет.", reply_markup=MENU)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Да, удалить все продажи", callback_data="reset_sales_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ])
    await update.message.reply_text(
        f"Вы действительно хотите удалить все записи о продажах ({count} шт.)?\n\n"
        "Будут удалены только операции типа «Продажа». Товары, себестоимость, возвраты и расходы останутся.",
        reply_markup=keyboard
    )

@user_database
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "unit_back":
        step = context.user_data.get("unit_step")
        if step is None:
            await query.message.reply_text("Расчёт не найден. Начните заново.", reply_markup=MENU)
            return
        if step <= 0:
            context.user_data.pop("flow", None)
            context.user_data.pop("unit_data", None)
            context.user_data.pop("unit_step", None)
            await query.message.reply_text("Возврат в главное меню.", reply_markup=MENU)
            return
        context.user_data["unit_step"] = step - 1
        await unit_prompt(query.message, context)
        return

    if data.startswith("unit_set:"):
        _, field, value = data.split(":", 2)
        d = context.user_data.setdefault("unit_data", {})
        if field == "scheme":
            d[field] = value
        elif field == "tax_system":
            d[field] = value
            context.user_data["unit_step"] = 7
            await query.message.reply_text(f"Шаг 7. Налоги ({value}). Выберите ставку:", reply_markup=unit_step_keyboard("tax_rate"))
            return
        elif field in ("tax_rate", "vat", "spp"):
            d[field] = float(value)
        context.user_data["unit_step"] = min(context.user_data.get("unit_step", 0) + 1, 12)
        if context.user_data["unit_step"] >= 12:
            await finish_unit_calc(query.message, context)
        else:
            await unit_prompt(query.message, context)
        return

    if data == "unit_manual_tax":
        context.user_data["flow"] = "unit_manual_tax_system"
        await query.message.reply_text("Введите название системы налогообложения:", reply_markup=INPUT_MENU)
        return
    if data.startswith("unit_manual:"):
        field = data.split(":", 1)[1]
        context.user_data["flow"] = "unit_manual_" + field
        await query.message.reply_text("Введите процент числом, например 6 или 20:", reply_markup=INPUT_MENU)
        return
    if data.startswith("unit_edit:"):
        field = data.split(":", 1)[1]
        fields = [x[0] for x in UNIT_STEPS]
        context.user_data["unit_step"] = fields.index(field)
        context.user_data["flow"] = "unit_calc"
        await unit_prompt(query.message, context)
        return
    if data == "unit_new":
        context.user_data["flow"] = "unit_calc"
        context.user_data["unit_data"] = {}
        context.user_data["unit_step"] = 0
        await unit_prompt(query.message, context)
        return
    if data == "unit_menu":
        context.user_data.clear()
        await query.message.reply_text("Главное меню.", reply_markup=MENU)
        return

    if data == "main_menu":
        context.user_data.clear()
        await query.message.reply_text("Главное меню.", reply_markup=MENU)
        return

    if data == "nav_back":
        if context.user_data.get("flow") == "batch_sale_qty":
            context.user_data.pop("batch_sale_selected", None)
            context.user_data.pop("flow", None)
            await send_batch_sale_picker(query.message, context)
        else:
            context.user_data.clear()
            await query.message.reply_text("Вернулись в главное меню.", reply_markup=MENU)
        return

    if data == "cancel":
        context.user_data.clear()
        await query.message.reply_text("Действие отменено.", reply_markup=MENU)
        return

    if data.startswith("stats:"):
        period = data.split(":", 1)[1]
        if period in ("day", "week", "month", "all"):
            await query.message.reply_text(stats_text(period), reply_markup=MENU)
        return


    if data.startswith("batch_sale:"):
        product = get_product(int(data.split(":", 1)[1]))
        if not product:
            await query.message.reply_text("Товар не найден. Выберите снова.", reply_markup=MENU)
            return
        context.user_data["batch_sale_selected"] = {
            "id": product["id"], "name": product["name"], "unit_cost": product["unit_cost"]
        }
        context.user_data["flow"] = "batch_sale_qty"
        await query.message.reply_text(
            f"Выбран товар: {product['name']}\n"
            f"Себестоимость: {money(product['unit_cost'])}/шт.\n"
            "Введите количество проданных штук:",
            reply_markup=INPUT_MENU
        )
        return

    if data == "batch_sale_done":
        items = context.user_data.get("batch_sale_items", [])
        if not items:
            await query.message.reply_text("Сначала добавьте хотя бы один товар.", reply_markup=MENU)
            return
        total_qty = 0
        total_cost = 0.0
        lines = []
        order_id = uuid.uuid4().hex
        for item in items:
            qty = int(item["qty"])
            amount = qty * float(item["unit_cost"])
            save_op("sale", item["name"], qty, amount, "Дневная продажа", order_id=order_id)
            total_qty += qty
            total_cost += amount
            lines.append(f"• {item['name']} — {qty} шт. = {money(amount)}")
        context.user_data.clear()
        await query.message.reply_text(
            "✅ Дневная продажа сохранена одной операцией ввода!\n\n"
            + "\n".join(lines)
            + f"\n\nВсего товаров: {total_qty} шт.\nОбщая себестоимость: {money(total_cost)}",
            reply_markup=MENU
        )
        return

    if data.startswith("sale:") or data.startswith("return:"):
        action, raw_id = data.split(":", 1)
        product = get_product(int(raw_id))
        if not product:
            await query.message.reply_text("Товар не найден. Обновите список.", reply_markup=MENU)
            return
        context.user_data["selected_product"] = {
            "id": product["id"],
            "name": product["name"],
            "unit_cost": product["unit_cost"]
        }
        context.user_data["flow"] = "sale_qty" if action == "sale" else "return_qty"
        label = "продано" if action == "sale" else "возвращено"
        await query.message.reply_text(
            f"Выбран товар: {product['name']}\n"
            f"Себестоимость: {money(product['unit_cost'])}/шт.\n\n"
            f"Введите количество, сколько {label}:",
            reply_markup=INPUT_MENU
        )
        return

    if data.startswith("cost:"):
        product = get_product(int(data.split(":", 1)[1]))
        if not product:
            await query.message.reply_text("Товар не найден.", reply_markup=MENU)
            return
        context.user_data["selected_product"] = {
            "id": product["id"], "name": product["name"], "unit_cost": product["unit_cost"]
        }
        context.user_data["flow"] = "new_cost"
        await query.message.reply_text(
            f"Товар: {product['name']}\nТекущая себестоимость: {money(product['unit_cost'])}\n\n"
            "Введите новую себестоимость за одну штуку:",
            reply_markup=INPUT_MENU
        )
        return

    if data.startswith("delete_product:"):
        product = get_product(int(data.split(":", 1)[1]))
        if not product:
            await query.message.reply_text("Товар не найден.", reply_markup=MENU)
            return
        context.user_data["pending_delete_product_id"] = product["id"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить товар", callback_data="delete_product_yes")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
        await query.message.reply_text(
            f"Удалить товар «{product['name']}» из каталога?\n\n"
            "Старые продажи и возвраты останутся в истории.",
            reply_markup=keyboard
        )
        return

    if data == "delete_product_yes":
        product_id = context.user_data.pop("pending_delete_product_id", None)
        if product_id is None:
            await query.message.reply_text("Не удалось определить товар. Начните заново.", reply_markup=MENU)
            return
        with connect() as c:
            cur = c.execute("DELETE FROM products WHERE id=?", (product_id,))
        await query.message.reply_text(
            "✅ Товар удалён из каталога. История операций сохранена." if cur.rowcount
            else "Товар уже отсутствует в каталоге.",
            reply_markup=MENU
        )
        return

    if data.startswith("op_delete:"):
        op_id = int(data.split(":", 1)[1])
        with connect() as c:
            op = c.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        if not op:
            await query.message.reply_text("Операция уже не найдена.", reply_markup=MENU)
            return
        context.user_data["pending_delete_op_id"] = op_id
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Удалить эту операцию", callback_data="op_delete_yes")],
            [InlineKeyboardButton("❌ Не удалять", callback_data="cancel")]
        ])
        await query.message.reply_text(
            f"Удалить операцию #{op_id}?\n"
            f"Тип: {op['kind']}\nТовар: {op['product_name'] or op['note']}\n"
            f"Количество: {op['qty']}\nСумма: {money(op['amount'])}\n\n"
            "После удаления статистика пересчитается автоматически.",
            reply_markup=keyboard
        )
        return

    if data == "op_delete_yes":
        op_id = context.user_data.pop("pending_delete_op_id", None)
        if op_id is None:
            await query.message.reply_text("Операция не выбрана.", reply_markup=MENU)
            return
        with connect() as c:
            cur = c.execute("DELETE FROM operations WHERE id=?", (op_id,))
        await query.message.reply_text(
            f"✅ Операция #{op_id} удалена. Статистика обновлена." if cur.rowcount
            else "Операция уже отсутствует.",
            reply_markup=MENU
        )
        return

    if data == "reset_sales_yes":
        with connect() as c:
            cur = c.execute("DELETE FROM operations WHERE kind='sale'")
            deleted = cur.rowcount
        context.user_data.clear()
        await query.message.reply_text(
            f"✅ Удалено записей о продажах: {deleted}.\n"
            "Товары, себестоимость, возвраты и расходы сохранены.",
            reply_markup=MENU
        )
        return

@user_database
async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    flow = context.user_data.get("flow")

    if text in ("❌ Отмена", "🏠 Главное меню"):
        context.user_data.clear()
        await update.message.reply_text(
            "Главное меню." if text == "🏠 Главное меню" else "Действие отменено.",
            reply_markup=MENU
        )
        return

    if text == "⬅️ Назад":
        if flow == "batch_sale_qty":
            context.user_data.pop("batch_sale_selected", None)
            context.user_data.pop("flow", None)
            await send_batch_sale_picker(update.message, context)
        else:
            context.user_data.clear()
            await update.message.reply_text("Вернулись в главное меню.", reply_markup=MENU)
        return

    # Если пользователь нажал другую кнопку меню посреди ввода — отменяем старый шаг
    menu_actions = {
        "📦 Товары", "➕ Добавить товар", "💰 Изменить себестоимость",
        "🗑 Удалить товар", "🛒 Продажа", "↩️ Возврат", "💸 Расход",
        "📊 Статистика", "📜 История", "🧹 Сбросить продажи", "🔎 Поиск товара", "📅 Продажа за день", "📊 Отчёт за дату", "🧮 Юнит-экономика", "🤖 ИИ-помощник"
    }
    if text in menu_actions:
        context.user_data.clear()
        if text == "📦 Товары":
            await show_products(update, context)
        elif text == "➕ Добавить товар":
            await add_product_start(update, context)
        elif text == "💰 Изменить себестоимость":
            await begin_cost_change(update, context)
        elif text == "🗑 Удалить товар":
            await begin_delete_product(update, context)
        elif text == "🛒 Продажа":
            await begin_sale(update, context)
        elif text == "📅 Продажа за день":
            await begin_batch_sale(update, context)
        elif text == "📊 Отчёт за дату":
            await begin_date_report(update, context)
        elif text == "🧮 Юнит-экономика":
            context.user_data["flow"] = "unit_calc"
            context.user_data["unit_data"] = {}
            context.user_data["unit_step"] = 0
            await unit_prompt(update.message, context)
        elif text == "↩️ Возврат":
            await begin_return(update, context)
        elif text == "💸 Расход":
            await begin_expense(update, context)
        elif text == "📊 Статистика":
            await show_stats(update, context)
        elif text == "📜 История":
            await show_history(update, context)
        elif text == "🧹 Сбросить продажи":
            await begin_reset_sales(update, context)
        elif text == "🔎 Поиск товара":
            await begin_search(update, context)
        elif text == "🤖 ИИ-помощник":
            await begin_ai_assistant(update, context)
        return

    if flow == "report_date":
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError:
            await update.message.reply_text("Неверный формат. Введите дату как 2026-11-02.")
            return
        context.user_data.clear()
        await update.message.reply_text(daily_report_text(day), reply_markup=MENU)
        return

    if flow == "unit_manual_tax_system":
        if len(text) > 30:
            await update.message.reply_text("Слишком длинное название. Введите коротко, например ОСНО.")
            return
        context.user_data.setdefault("unit_data", {})["tax_system"] = text
        context.user_data["unit_step"] = 7
        context.user_data["flow"] = "unit_calc"
        await update.message.reply_text("Введите ставку налога в процентах:", reply_markup=INPUT_MENU)
        context.user_data["flow"] = "unit_manual_tax_rate"
        return

    if flow and flow.startswith("unit_manual_"):
        field = flow.replace("unit_manual_", "")
        try:
            value = float(text.replace(" ", "").replace(",", ".").replace("%", ""))
            if value < 0 or value > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите процент от 0 до 100, например 6.")
            return
        context.user_data.setdefault("unit_data", {})[field] = value
        context.user_data["flow"] = "unit_calc"
        context.user_data["unit_step"] = 8 if field == "tax_rate" else context.user_data.get("unit_step", 0) + 1
        await unit_prompt(update.message, context)
        return

    if flow == "unit_calc":
        step = context.user_data.get("unit_step", 0)
        field = UNIT_STEPS[step][0]
        d = context.user_data.setdefault("unit_data", {})
        try:
            if field == "category":
                if not text or len(text) > 120: raise ValueError
                d[field] = text
            elif field in ("cost", "drr", "spp", "buyout", "price", "packing", "extra"):
                value = float(text.replace(" ", "").replace(",", ".").replace("₽", "").replace("%", ""))
                if value < 0 or (field in ("drr", "spp", "buyout") and value > 100): raise ValueError
                if field == "buyout" and value == 0: raise ValueError
                d[field] = value
            elif field == "volume":
                d[field] = parse_volume(text)
            elif field == "tax_rate":
                value = float(text.replace(",", ".").replace("%", ""))
                if not 0 <= value <= 100: raise ValueError
                d[field] = value
            elif field in ("scheme", "tax_system", "vat"):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Проверьте значение и попробуйте ещё раз.")
            return
        context.user_data["unit_step"] = step + 1
        if context.user_data["unit_step"] >= len(UNIT_STEPS):
            await finish_unit_calc(update.message, context)
        else:
            await unit_prompt(update.message, context)
        return

    if flow == "add_name":
        if not text or len(text) > 100:
            await update.message.reply_text("Введите название до 100 символов.")
            return
        context.user_data["new_product_name"] = text
        context.user_data["flow"] = "add_cost"
        await update.message.reply_text("Введите себестоимость одной штуки в рублях:", reply_markup=INPUT_MENU)
        return

    if flow in ("add_cost", "new_cost", "expense_amount"):
        try:
            value = float(text.replace(" ", "").replace(",", ".").replace("₽", ""))
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите неотрицательное число, например 1250 или 1250,50.")
            return

        if flow == "add_cost":
            name = context.user_data.get("new_product_name")
            try:
                with connect() as c:
                    c.execute(
                        "INSERT INTO products(name, unit_cost, created_at) VALUES(?,?,?)",
                        (name, value, now())
                    )
            except sqlite3.IntegrityError:
                await update.message.reply_text("Товар с таким названием уже существует.", reply_markup=MENU)
                context.user_data.clear()
                return
            await update.message.reply_text(
                f"✅ Товар добавлен: {name}\nСебестоимость: {money(value)}/шт.",
                reply_markup=MENU
            )
        elif flow == "new_cost":
            p = context.user_data.get("selected_product")
            if not p:
                await update.message.reply_text("Товар не выбран. Начните заново.", reply_markup=MENU)
                context.user_data.clear()
                return
            with connect() as c:
                c.execute("UPDATE products SET unit_cost=? WHERE id=?", (value, p["id"]))
            await update.message.reply_text(
                f"✅ Себестоимость товара «{p['name']}» изменена.\n"
                f"Было: {money(p['unit_cost'])}\nСтало: {money(value)} за штуку.\n\n"
                "Новые продажи будут считаться по новой себестоимости; старые записи не изменятся.",
                reply_markup=MENU
            )
        else:
            note = context.user_data.get("expense_name", "Расход")
            save_op("expense", None, 0, value, note)
            await update.message.reply_text(f"✅ Расход записан: {note} — {money(value)}", reply_markup=MENU)
        context.user_data.clear()
        return


    if flow == "batch_sale_qty":
        try:
            qty = int(text)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите целое количество больше нуля.")
            return
        product = context.user_data.get("batch_sale_selected")
        if not product:
            await update.message.reply_text("Товар не выбран. Начните дневную продажу заново.", reply_markup=MENU)
            context.user_data.clear()
            return
        cart = context.user_data.setdefault("batch_sale_items", [])
        existing = next((item for item in cart if item["id"] == product["id"]), None)
        if existing:
            existing["qty"] += qty
        else:
            cart.append({**product, "qty": qty})
        context.user_data.pop("batch_sale_selected", None)
        context.user_data.pop("flow", None)
        await send_batch_sale_picker(update.message, context)
        return

    if flow in ("sale_qty", "return_qty"):
        try:
            qty = int(text)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите целое количество больше нуля.")
            return
        p = context.user_data.get("selected_product")
        if not p:
            await update.message.reply_text("Товар не выбран. Начните заново.", reply_markup=MENU)
            context.user_data.clear()
            return
        amount = qty * float(p["unit_cost"])
        kind = "sale" if flow == "sale_qty" else "return"
        save_op(kind, p["name"], qty, amount, order_id=uuid.uuid4().hex if kind == "sale" else None)
        if kind == "sale":
            message = (
                f"✅ Продажа записана\n{p['name']} — {qty} шт.\n"
                f"{money(p['unit_cost'])} × {qty} = {money(amount)}"
            )
        else:
            message = (
                f"↩️ Возврат записан\n{p['name']} — {qty} шт.\n"
                f"Себестоимость возврата: {money(amount)}"
            )
        context.user_data.clear()
        await update.message.reply_text(message, reply_markup=MENU)
        return

    if flow == "expense_name":
        context.user_data["expense_name"] = text[:100]
        context.user_data["flow"] = "expense_amount"
        await update.message.reply_text("Введите сумму расхода в рублях:", reply_markup=INPUT_MENU)
        return

    if flow == "ai_question":
        await update.message.reply_text("🤖 Думаю над ответом…", reply_markup=INPUT_MENU)
        answer = await ask_ai(text, context)
        await update.message.reply_text(answer, reply_markup=INPUT_MENU)
        return

    if flow == "search":
        q = text.casefold()
        rows = [r for r in product_rows() if q in r["name"].casefold()]
        context.user_data.clear()
        if rows:
            answer = "🔎 Найдено:\n\n" + "\n".join(
                f"• {r['name']} — {money(r['unit_cost'])}/шт." for r in rows
            )
        else:
            answer = "Совпадений не найдено."
        await update.message.reply_text(answer, reply_markup=MENU)
        return

    await update.message.reply_text("Выберите действие в меню ниже.", reply_markup=MENU)

@user_database
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Используйте кнопки меню. «⬅️ Назад» возвращает к предыдущему шагу, «🏠 Главное меню» — в меню.\n"
        "В истории можно удалить отдельную ошибочную операцию. Для ИИ-помощника нужен OPENAI_API_KEY в Bothost.",
        reply_markup=MENU
    )

@user_database
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: {update.effective_user.id}\\n"
        "Этот номер можно указать в OWNER_TELEGRAM_ID в Bothost, "
        "чтобы сохранить доступ к прежней базе владельца."
    )


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.run_polling()

if __name__ == "__main__":
    main()
