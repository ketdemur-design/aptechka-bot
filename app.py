import os
import math
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Хранилище (позже заменим на БД)
data_store = {}
user_states = {}

FORMS = {
    "tablets": ("таблеток", "таблетке"),
    "capsules": ("капсул", "капсуле"),
    "sachets": ("саше", "саше"),
    "liquid": ("мл", "мл")
}

# ================== МЕНЮ ==================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")]
    ])

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу следить за лекарствами 💊\n\n"
        "Выберите действие 👇",
        reply_markup=main_menu()
    )

# ================== ДОБАВЛЕНИЕ ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_states.get(chat_id)
    if not state:
        return

    text = update.message.text.strip()
    data = state["data"]

    if state["step"] == "name":
        data["name"] = text
        state["step"] = "form"
        await update.message.reply_text(
            "Выберите форму лекарства:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Таблетки", callback_data="form_tablets")],
                [InlineKeyboardButton("Капсулы", callback_data="form_capsules")],
                [InlineKeyboardButton("Саше", callback_data="form_sachets")],
                [InlineKeyboardButton("Жидкая форма", callback_data="form_liquid")]
            ])
        )

    elif state["step"] == "unit_mg":
        data["unit_mg"] = float(text.replace(",", "."))
        state["step"] = "bought"
        plural, _ = FORMS[data["form"]]
        await update.message.reply_text(f"Сколько {plural} купили?")

    elif state["step"] == "bought":
        data["bought_units"] = float(text.replace(",", "."))
        state["step"] = "daily_mg"
        await update.message.reply_text("Сколько мг в сутки вам назначено?")

    elif state["step"] == "daily_mg":
        data["daily_mg"] = float(text.replace(",", "."))
        state["step"] = "course"
        await update.message.reply_text(
            "На какой срок назначено?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Дни", callback_data="course_days")],
                [InlineKeyboardButton("Месяцы", callback_data="course_months")],
                [InlineKeyboardButton("Пожизненно", callback_data="course_life")]
            ])
        )

    elif state["step"] == "course_value":
        value = float(text.replace(",", "."))
        if data["course_type"] == "months":
            data["course_days"] = value * 30
        else:
            data["course_days"] = value
        await save_medicine(update, chat_id)

# ================== КНОПКИ ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    if query.data == "add":
        await add_start(update, context)

    elif query.data.startswith("form_"):
        form = query.data.replace("form_", "")
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"
        _, single = FORMS[form]
        await query.message.reply_text(f"Сколько мг в одной {single}?")

    elif query.data.startswith("course_"):
        if query.data == "course_life":
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(update, chat_id)
        else:
            user_states[chat_id]["data"]["course_type"] = query.data.replace("course_", "")
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif query.data == "summary":
        await summary(update, context)

    elif query.data == "forecast":
        await forecast(update, context)

# ================== СОХРАНЕНИЕ ==================

async def save_medicine(update: Update, chat_id):
    data = user_states[chat_id]["data"]

    total_mg = data["unit_mg"] * data["bought_units"]
    days_available = total_mg / data["daily_mg"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        **data,
        "total_mg": total_mg,
        "created": datetime.now()
    }

    msg = f"✅ {data['name']} добавлен.\nХватит примерно на {math.floor(days_available)} дней."

    user_states.pop(chat_id)
    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== СВОДКА ==================

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    if not meds:
        await update.effective_message.reply_text("Список пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка:\n\n"
    for m in meds.values():
        days = int(m["total_mg"] / m["daily_mg"])
        msg += f"{m['name']} — осталось {days} дней\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== ПРОГНОЗ ==================

async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"

    for m in meds.values():
        days = int(m["total_mg"] / m["daily_mg"])
        end = m["created"] + timedelta(days=days)
        msg += f"{m['name']} — закончится {end.strftime('%d.%m.%Y')}\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== ЗАПУСК ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🤖 Бот запущен (polling)")
    app.run_polling()

if __name__ == "__main__":
    main()
