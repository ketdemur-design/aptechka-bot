import os
import asyncio
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

data_store = {}
user_states = {}

FORMS = {
    "form_tablets": ("таблетке", "таблеток"),
    "form_capsules": ("капсуле", "капсул"),
    "form_sachet": ("саше", "саше"),
    "form_liquid": ("бутылке", "бутылок"),
}

# ================== МЕНЮ ==================

def start_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]])

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("🔧 Изменить дозировку", callback_data="dose")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

def form_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets")],
        [InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet")],
        [InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
    ])

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни", callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

# ================== HELPERS ==================

def calc_days_left(med):
    days_by_stock = med["total_mg"] / med["daily_mg"]

    if med["course_end"]:
        days_by_course = (med["course_end"] - datetime.now()).days
        return max(0, int(min(days_by_stock, days_by_course)))

    return max(0, int(days_by_stock))

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋\n\n"
        "Я помогу:\n"
        "• следить за лекарствами 💊\n"
        "• учитывать дозировки\n"
        "• пересчитывать остатки\n"
        "• напоминать о покупке\n\n"
        "Нажми «Начать» 👇",
        reply_markup=start_menu()
    )

# ================== TEXT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.replace(",", ".").strip()

    state = user_states.get(chat_id)
    if not state:
        return

    d = state["data"]

    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text)
            state["step"] = "units"
            one, many = FORMS[d["form"]]
            await update.message.reply_text(f"Сколько {many} купили?")

        elif state["step"] == "units":
            d["units"] = int(float(text))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма?", reply_markup=course_menu())

        elif state["step"] == "course_value":
            value = int(float(text))
            if d["course_type"] == "days":
                d["course_end"] = datetime.now() + timedelta(days=value)
            else:
                d["course_end"] = datetime.now() + timedelta(days=value * 30)
            await save_medicine(update, chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "start_bot":
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")

    elif data.startswith("form_"):
        one, many = FORMS[data]
        state = user_states[chat_id]
        state["data"]["form"] = data
        state["step"] = "unit_mg"
        await query.message.reply_text(f"Сколько мг в одной {one}?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        if data.endswith("forever"):
            state["data"]["course_end"] = None
            await save_medicine(query, chat_id)
        else:
            state["data"]["course_type"] = data.split("_")[1]
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data in ("summary", "forecast"):
        await (show_summary if data == "summary" else show_forecast)(query)

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    total_mg = d["unit_mg"] * d["units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "form": d["form"],
        "daily_mg": d["daily_mg"],
        "total_mg": total_mg,
        "created": datetime.now(),
        "course_end": d.get("course_end"),
        "notified": False,
        "purchases": {d["unit_mg"]: d["units"]},
    }

    days = calc_days_left(data_store[chat_id][d["name"]])

    await update.message.reply_text(
        f"✅ Лекарство добавлено\n"
        f"Название: {d['name']}\n"
        f"Хватит на: {days} дней",
        reply_markup=main_menu()
    )
    user_states.pop(chat_id)

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "📋 Сводка:\n\n"
    for name, m in meds.items():
        msg += f"{name} — {calc_days_left(m)} дней\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

async def show_forecast(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "⏳ Прогноз:\n\n"
    for name, m in meds.items():
        if m["course_end"]:
            end = min(
                m["course_end"],
                m["created"] + timedelta(days=m["total_mg"] / m["daily_mg"])
            )
            msg += f"{name} — до {end.strftime('%d.%m.%Y')}\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== REMINDER ==================

async def reminder_loop(app):
    while True:
        for chat_id, meds in data_store.items():
            for name, m in meds.items():
                if not m["notified"] and m["course_end"]:
                    days = calc_days_left(m)
                    if 0 < days <= 7:
                        await app.bot.send_message(
                            chat_id,
                            f"🛒 Заканчивается {name}\n"
                            f"Хватит на: {days} дней"
                        )
                        m["notified"] = True
        await asyncio.sleep(86400)

# ================== MAIN ==================

async def post_init(app):
    app.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
