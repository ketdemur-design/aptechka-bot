import os
import math
import asyncio
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== TOKEN ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

# ================== ХРАНИЛИЩА ==================

data_store = {}      # chat_id -> medicines
user_states = {}     # chat_id -> state

# ================== СПРАВОЧНИКИ ==================

FORMS = {
    "tablets": ("таблетки", "таблеток"),
    "capsules": ("капсулы", "капсул"),
    "sachets": ("саше", "саше"),
    "liquid": ("мл", "мл"),
}

# ================== МЕНЮ ==================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("🔧 Изменить дозировку", callback_data="dose")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋\nЯ помогу учитывать лекарства.\n\nВыберите действие:",
        reply_markup=main_menu()
    )

# ================== ADD ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

def form_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Таблетки", callback_data="form_tablets")],
        [InlineKeyboardButton("Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("Саше", callback_data="form_sachets")],
        [InlineKeyboardButton("Жидкая форма", callback_data="form_liquid")],
    ])

def course_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Дни", callback_data="course_days")],
        [InlineKeyboardButton("Месяцы", callback_data="course_months")],
        [InlineKeyboardButton("Пожизненно", callback_data="course_life")],
    ])

# ================== TEXT HANDLER ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_states.get(chat_id)
    if not state:
        return

    text = update.message.text.replace(",", ".").strip()
    data = state["data"]

    if state["flow"] == "add":
        if state["step"] == "name":
            data["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_keyboard())

        elif state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "bought_units"
            await update.message.reply_text("Сколько штук купили?")

        elif state["step"] == "bought_units":
            data["bought_units"] = float(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки вы принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("На какой срок приём?", reply_markup=course_keyboard())

        elif state["step"] == "course_value":
            value = float(text)
            data["course_days"] = value * 30 if data["course_type"] == "months" else value
            await save_medicine(update, chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "add":
        await add_start(update, context)

    elif data.startswith("form_"):
        form = data.replace("form_", "")
        state = user_states[chat_id]
        state["data"]["form"] = form
        state["step"] = "unit_mg"
        await query.message.reply_text("Сколько мг в одной таблетке?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        if data == "course_life":
            state["data"]["course_days"] = None
            await save_medicine(update, chat_id)
        else:
            state["data"]["course_type"] = data.replace("course_", "")
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "summary":
        await show_summary(query)

    elif data == "forecast":
        await show_forecast(query)

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]
    total_mg = data["unit_mg"] * data["bought_units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "unit_mg": data["unit_mg"],
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "course_days": data.get("course_days"),
        "created": datetime.now(),
        "notified": False,
    }

    await update.effective_message.reply_text(
        f"✅ {data['name']} добавлен",
        reply_markup=main_menu()
    )
    user_states.pop(chat_id)

# ================== SUMMARY ==================

async def show_summary(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        msg += f"{name} — {days} дней\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== FORECAST ==================

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"
    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        end = med["created"] + timedelta(days=days)
        msg += f"{name} — закончится {end.strftime('%d.%m.%Y')}\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== НАПОМИНАНИЯ ==================

async def reminder_loop(app):
    while True:
        for chat_id, meds in data_store.items():
            for name, med in meds.items():
                if med["notified"]:
                    continue

                days_left = med["total_mg"] / med["daily_mg"]

                if days_left <= 7:
                    needed_mg = math.ceil((7 - days_left) * med["daily_mg"])
                    if needed_mg <= 0:
                        continue

                    t250 = math.ceil(needed_mg / 250)
                    t500 = math.ceil(needed_mg / 500)

                    text = (
                        f"🛒 Пора купить **{name}**\n\n"
                        f"Нужно докупить {needed_mg} мг:\n"
                        f"• 250 мг — {t250} таблеток\n"
                        f"• 500 мг — {t500} таблеток"
                    )

                    await app.bot.send_message(chat_id, text)
                    med["notified"] = True

        await asyncio.sleep(24 * 60 * 60)

async def post_init(app):
    app.create_task(reminder_loop(app))

# ================== MAIN ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()

if __name__ == "__main__":
    main()
