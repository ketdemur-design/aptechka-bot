import os
import math
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== НАСТРОЙКИ ==================

TOKEN = os.getenv("BOT_TOKEN")

data_store = {}
user_states = {}

FORMS = {
    "tablets": "таблеток",
    "sachets": "саше",
    "capsules": "капсул",
}

# ================== МЕНЮ ==================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
    ])

# ================== /start ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для учёта лекарств 💊\n\n"
        "Что вы хотите сделать дальше?",
        reply_markup=main_menu()
    )

# ================== ДОБАВЛЕНИЕ ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    user_states[chat_id] = {"step": "name", "data": {}}

    await query.message.reply_text("Введите название лекарства:")

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
            "Выберите форму:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Таблетки", callback_data="form_tablets")],
                [InlineKeyboardButton("Саше", callback_data="form_sachets")],
                [InlineKeyboardButton("Капсулы", callback_data="form_capsules")],
            ])
        )

    elif state["step"] == "unit_mg":
        data["unit_mg"] = float(text.replace(",", "."))
        state["step"] = "bought"

        await update.message.reply_text(
            f"Сколько {FORMS[data['form']]} вы купили?"
        )

    elif state["step"] == "bought":
        data["bought_units"] = float(text.replace(",", "."))
        state["step"] = "daily_mg"
        await update.message.reply_text("Сколько мг в сутки вам назначено?")

    elif state["step"] == "daily_mg":
        data["daily_mg"] = float(text.replace(",", "."))
        await save_medicine(update, chat_id)

# ================== КНОПКИ ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    data = query.data

    if data == "add":
        await add_start(update, context)

    elif data.startswith("form_"):
        form = data.replace("form_", "")
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"

        await query.message.reply_text(
            "Сколько мг в одной таблетке?"
            if form == "tablets"
            else "Сколько мг в одной единице?"
        )

    elif data == "summary":
        await summary(update, context)

    elif data == "forecast":
        await forecast(update, context)

# ================== СОХРАНЕНИЕ ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]

    total_mg = data["unit_mg"] * data["bought_units"]
    days_available = total_mg / data["daily_mg"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "created": datetime.now(),
    }

    user_states.pop(chat_id)

    await update.message.reply_text(
        f"✅ {data['name']} добавлен.\n"
        f"Хватит на {math.floor(days_available)} дней.",
        reply_markup=main_menu()
    )

# ================== СВОДКА ==================

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    if not meds:
        await update.message.reply_text("Список пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = int(med["total_mg"] / med["daily_mg"])
        msg += f"{name} — осталось {days} дней\n"

    await update.message.reply_text(msg, reply_markup=main_menu())

# ================== ПРОГНОЗ ==================

async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    msg = "⏳ Прогноз:\n\n"
    for name, med in meds.items():
        days = int(med["total_mg"] / med["daily_mg"])
        end = med["created"] + timedelta(days=days)
        msg += f"{name} — закончится {end:%d.%m.%Y}\n"

    await update.message.reply_text(msg, reply_markup=main_menu())

# ================== ЗАПУСК ==================

def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.run_polling()

if __name__ == "__main__":
    main()

