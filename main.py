import os
import sqlite3
import httpx
from datetime import datetime, date, timedelta
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
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the hosting panel environment variables.")

# ВАЖНО: путь к базе оставлен таким же, как в предыдущей версии.
DATA_DIR = Path("/app/data")
if not DATA_DIR.exists():
    DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ozonamon.db"

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📦 Товары"), KeyboardButton("➕ Добавить товар")],
        [KeyboardButton("💰 Изменить себестоимость"), KeyboardButton("🗑 Удалить товар")],
        [KeyboardButton("🛒 Продажа"), KeyboardButton("↩️ Возврат")],
        [KeyboardButton("💸 Расход"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("📜 История"), KeyboardButton("🧹 Сбросить продажи")],
        [KeyboardButton("📅 Продажа за день")],
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
    conn = sqlite3.connect(DB_PATH)
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
            created_at TEXT NOT NULL
        )""")

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

def save_op(kind, product_name=None, qty=0, amount=0, note=None):
    with connect() as c:
        cur = c.execute(
            """INSERT INTO operations
            (kind, product_name, qty, amount, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, product_name, int(qty), float(amount), note, now())
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Добро пожаловать в OZONAMON 10!\n\n"
        "Теперь товары для продаж и возвратов выбираются кнопками. "
        "Себестоимость можно менять, а ошибочные операции — удалять.",
        reply_markup=MENU
    )

async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Каталог пуст. Нажмите «➕ Добавить товар».", reply_markup=MENU)
        return
    lines = [f"• {r['name']} — {money(r['unit_cost'])}/шт." for r in rows]
    await update.message.reply_text("📦 Товары в каталоге:\n\n" + "\n".join(lines), reply_markup=MENU)

async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["flow"] = "add_name"
    await update.message.reply_text("Введите название нового товара:", reply_markup=INPUT_MENU)

async def begin_product_action(query, context, action, title):
    keyboard = product_keyboard(action)
    if not keyboard:
        await query.message.reply_text("В каталоге пока нет товаров.", reply_markup=MENU)
        return
    await query.message.reply_text(title, reply_markup=keyboard)

async def begin_sale(update, context):
    await update.message.reply_text(
        "🛒 Выберите товар для продажи:",
        reply_markup=product_keyboard("sale") or cancel_keyboard()
    )

async def begin_return(update, context):
    await update.message.reply_text(
        "↩️ Выберите товар, который вернули:",
        reply_markup=product_keyboard("return") or cancel_keyboard()
    )

async def begin_cost_change(update, context):
    await update.message.reply_text(
        "💰 Выберите товар, у которого нужно изменить себестоимость:",
        reply_markup=product_keyboard("cost") or cancel_keyboard()
    )

async def begin_delete_product(update, context):
    await update.message.reply_text(
        "🗑 Выберите товар для удаления:",
        reply_markup=product_keyboard("delete_product") or cancel_keyboard()
    )

async def begin_expense(update, context):
    context.user_data["flow"] = "expense_name"
    await update.message.reply_text(
        "Напишите название расхода (например, доставка или упаковка):",
        reply_markup=INPUT_MENU
    )

async def begin_search(update, context):
    context.user_data["flow"] = "search"
    await update.message.reply_text("Введите название или часть названия товара:", reply_markup=INPUT_MENU)


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

async def show_stats(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Сегодня", callback_data="stats:day"),
         InlineKeyboardButton("7 дней", callback_data="stats:week")],
        [InlineKeyboardButton("Этот месяц", callback_data="stats:month"),
         InlineKeyboardButton("Всё время", callback_data="stats:all")],
        *NAV_BUTTONS
    ])
    await update.message.reply_text(stats_text("day"), reply_markup=keyboard)

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

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

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
        for item in items:
            qty = int(item["qty"])
            amount = qty * float(item["unit_cost"])
            save_op("sale", item["name"], qty, amount, "Дневная продажа")
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
        "📊 Статистика", "📜 История", "🧹 Сбросить продажи", "🔎 Поиск товара", "📅 Продажа за день", "🤖 ИИ-помощник"
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
        save_op(kind, p["name"], qty, amount)
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Используйте кнопки меню. «⬅️ Назад» возвращает к предыдущему шагу, «🏠 Главное меню» — в меню.\n"
        "В истории можно удалить отдельную ошибочную операцию. Для ИИ-помощника нужен OPENAI_API_KEY в Bothost.",
        reply_markup=MENU
    )

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.run_polling()

if __name__ == "__main__":
    main()
