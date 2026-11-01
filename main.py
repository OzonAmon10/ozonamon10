import os
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the hosting panel environment variables.")

DATA_DIR = Path("/app/data")
if not DATA_DIR.exists():
    DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ozonamon.db"

ADD_NAME, ADD_COST, SALE_NAME, SALE_QTY, RETURN_NAME, RETURN_QTY, EXPENSE_NAME, EXPENSE_AMOUNT = range(8)

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📦 Товары"), KeyboardButton("➕ Добавить товар")],
        [KeyboardButton("🛒 Продажа"), KeyboardButton("↩️ Возврат")],
        [KeyboardButton("💸 Расход"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("🔎 Поиск товара"), KeyboardButton("📜 История")],
    ],
    resize_keyboard=True
)

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

def money(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"

def product_rows():
    with connect() as c:
        return c.execute("SELECT * FROM products ORDER BY name").fetchall()

def get_product(name):
    with connect() as c:
        return c.execute("SELECT * FROM products WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()

def save_op(kind, product_name=None, qty=0, amount=0, note=None):
    with connect() as c:
        c.execute(
            "INSERT INTO operations(kind, product_name, qty, amount, note, created_at) VALUES(?,?,?,?,?,?)",
            (kind, product_name, int(qty), float(amount), note, now())
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
    label = {"day":"сегодня", "week":"за последние 7 дней", "month":"с начала месяца", "all":"за всё время"}[period]
    with connect() as c:
        rows = c.execute("SELECT * FROM operations WHERE date(created_at) >= date(?) ORDER BY created_at", (start,)).fetchall()
    sold_qty = sum(r["qty"] for r in rows if r["kind"] == "sale")
    returned_qty = sum(r["qty"] for r in rows if r["kind"] == "return")
    sold_cost = sum(r["amount"] for r in rows if r["kind"] == "sale")
    returned_cost = sum(r["amount"] for r in rows if r["kind"] == "return")
    expenses = sum(r["amount"] for r in rows if r["kind"] == "expense")
    net_cost = sold_cost - returned_cost + expenses
    return (
        f"📊 Статистика {label}\n\n"
        f"Продано штук: {sold_qty}\n"
        f"Возвращено штук: {returned_qty}\n"
        f"Себестоимость продаж: {money(sold_cost)}\n"
        f"Себестоимость возвратов: −{money(returned_cost)}\n"
        f"Расходы: {money(expenses)}\n"
        f"Итого (себестоимость продаж − возвраты + расходы): {money(net_cost)}\n\n"
        "Это учёт себестоимости и расходов, не выручки и не бухгалтерская чистая прибыль."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добро пожаловать в OZONAMON 10!\n\n"
        "Добавьте товары с себестоимостью одной штуки, затем записывайте продажи, возвраты и расходы.\n"
        "Данные сохраняются в базе бота.",
        reply_markup=MENU
    )

async def products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Каталог пока пуст. Нажмите «➕ Добавить товар».", reply_markup=MENU)
        return
    text = "📦 Ваши товары:\n\n" + "\n".join(f"• {r['name']} — {money(r['unit_cost'])}/шт." for r in rows)
    await update.message.reply_text(text, reply_markup=MENU)

async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите название нового товара:")
    return ADD_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) > 100:
        await update.message.reply_text("Слишком длинное название. Введите до 100 символов.")
        return ADD_NAME
    context.user_data["new_product_name"] = name
    await update.message.reply_text("Введите себестоимость ОДНОЙ штуки в рублях (например, 1450):")
    return ADD_COST

async def add_product_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost = float(update.message.text.strip().replace(" ", "").replace(",", ".").replace("₽", ""))
        if cost < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Введите неотрицательное число, например 1450 или 1450,50.")
        return ADD_COST
    name = context.user_data["new_product_name"]
    try:
        with connect() as c:
            c.execute("INSERT INTO products(name, unit_cost, created_at) VALUES(?,?,?)", (name, cost, now()))
    except sqlite3.IntegrityError:
        await update.message.reply_text("Товар с таким названием уже есть. Используйте другое название.")
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Добавлен товар: {name}\nСебестоимость одной штуки: {money(cost)}", reply_markup=MENU)
    return ConversationHandler.END

async def choose_sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Сначала добавьте товар через «➕ Добавить товар».", reply_markup=MENU)
        return ConversationHandler.END
    await update.message.reply_text("Введите точное название товара:\n" + "\n".join(f"• {r['name']}" for r in rows))
    return SALE_NAME

async def sale_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_product(update.message.text)
    if not p:
        await update.message.reply_text("Не нашёл товар. Проверьте название или используйте «📦 Товары».")
        return SALE_NAME
    context.user_data["sale_product"] = dict(p)
    await update.message.reply_text(f"{p['name']}\nСебестоимость: {money(p['unit_cost'])}/шт.\nСколько штук продано?")
    return SALE_QTY

async def sale_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
        if qty <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Введите целое количество больше нуля.")
        return SALE_QTY
    p = context.user_data["sale_product"]
    amount = qty * p["unit_cost"]
    save_op("sale", p["name"], qty, amount)
    await update.message.reply_text(
        f"✅ Продажа записана\n{p['name']}: {qty} шт.\n"
        f"Себестоимость: {money(p['unit_cost'])} × {qty} = {money(amount)}\n\n"
        + stats_text("day"), reply_markup=MENU
    )
    return ConversationHandler.END

async def return_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = product_rows()
    if not rows:
        await update.message.reply_text("Каталог пока пуст.", reply_markup=MENU)
        return ConversationHandler.END
    await update.message.reply_text("Какой товар вернули?\n" + "\n".join(f"• {r['name']}" for r in rows))
    return RETURN_NAME

async def return_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_product(update.message.text)
    if not p:
        await update.message.reply_text("Не нашёл товар. Введите название из каталога.")
        return RETURN_NAME
    context.user_data["return_product"] = dict(p)
    await update.message.reply_text(f"Себестоимость {p['name']}: {money(p['unit_cost'])}/шт.\nСколько штук вернули?")
    return RETURN_QTY

async def return_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
        if qty <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Введите целое количество больше нуля.")
        return RETURN_QTY
    p = context.user_data["return_product"]
    amount = qty * p["unit_cost"]
    save_op("return", p["name"], qty, amount)
    await update.message.reply_text(f"↩️ Возврат записан: {p['name']} — {qty} шт.\nСебестоимость возврата: {money(amount)}", reply_markup=MENU)
    return ConversationHandler.END

async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напишите название расхода (например, доставка или упаковка):")
    return EXPENSE_NAME

async def expense_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["expense_name"] = update.message.text.strip()
    await update.message.reply_text("Введите сумму расхода в рублях:")
    return EXPENSE_AMOUNT

async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(" ", "").replace(",", ".").replace("₽", ""))
        if amount < 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например 500 или 500,50.")
        return EXPENSE_AMOUNT
    name = context.user_data["expense_name"]
    save_op("expense", None, 0, amount, name)
    await update.message.reply_text(f"✅ Расход записан: {name} — {money(amount)}", reply_markup=MENU)
    return ConversationHandler.END

async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите часть названия товара для поиска:")
    return 20

async def search_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip().lower()
    rows = [r for r in product_rows() if q in r["name"].lower()]
    if rows:
        msg = "🔎 Результаты поиска:\n\n" + "\n".join(f"• {r['name']} — {money(r['unit_cost'])}/шт." for r in rows)
    else:
        msg = "Совпадений не найдено."
    await update.message.reply_text(msg, reply_markup=MENU)
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите период:\n\n" + stats_text("day") + "\n\n"
        "Команды для других периодов: /week, /month, /all",
        reply_markup=MENU
    )

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text("week"), reply_markup=MENU)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text("month"), reply_markup=MENU)

async def all_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text("all"), reply_markup=MENU)

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with connect() as c:
        rows = c.execute("SELECT * FROM operations ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        await update.message.reply_text("Операций пока нет.", reply_markup=MENU)
        return
    lines = []
    for r in rows:
        dt = r["created_at"].replace("T", " ")
        if r["kind"] == "sale":
            lines.append(f"{dt} · Продажа · {r['product_name']} · {r['qty']} шт. · {money(r['amount'])}")
        elif r["kind"] == "return":
            lines.append(f"{dt} · Возврат · {r['product_name']} · {r['qty']} шт. · {money(r['amount'])}")
        else:
            lines.append(f"{dt} · Расход · {r['note']} · {money(r['amount'])}")
    await update.message.reply_text("📜 Последние 20 операций:\n\n" + "\n".join(lines), reply_markup=MENU)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📦 Товары":
        return await products(update, context)
    if text == "➕ Добавить товар":
        return await add_product_start(update, context)
    if text == "🛒 Продажа":
        return await choose_sale_start(update, context)
    if text == "↩️ Возврат":
        return await return_start(update, context)
    if text == "💸 Расход":
        return await expense_start(update, context)
    if text == "📊 Статистика":
        return await stats(update, context)
    if text == "🔎 Поиск товара":
        return await search_start(update, context)
    if text == "📜 История":
        return await history(update, context)
    await update.message.reply_text("Выберите действие в меню ниже или используйте /help.", reply_markup=MENU)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OZONAMON 10 — команды статистики:\n"
        "/stats — за сегодня\n/week — последние 7 дней\n/month — с начала месяца\n/all — всё время\n"
        "Для операций используйте кнопки меню.", reply_markup=MENU
    )

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("all", all_stats))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить товар$"), add_product_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_cost)],
        },
        fallbacks=[CommandHandler("cancel", start)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🛒 Продажа$"), choose_sale_start)],
        states={
            SALE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_name)],
            SALE_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_qty)],
        },
        fallbacks=[CommandHandler("cancel", start)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^↩️ Возврат$"), return_start)],
        states={
            RETURN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, return_name)],
            RETURN_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, return_qty)],
        },
        fallbacks=[CommandHandler("cancel", start)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Расход$"), expense_start)],
        states={
            EXPENSE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_name)],
            EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
        },
        fallbacks=[CommandHandler("cancel", start)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔎 Поиск товара$"), search_start)],
        states={20: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_results)]},
        fallbacks=[CommandHandler("cancel", start)],
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.run_polling()

if __name__ == "__main__":
    main()
