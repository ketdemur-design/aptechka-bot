import os
import math
import asyncio
from datetime import datetime, timedelta

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# ================== ХРАНИЛИЩА ==================

data_store = {}     # chat_id -> лекарства
user_states = {}    # chat_id -> состояние диалога

# ================== СПРАВОЧНИКИ ==================

FORM_UNIT_MG = {
    "tablets": "таблетке",
    "capsules": "капсуле",
    "sachets": "саше",
    "liquid": "1 мл",
}

FORM_BUY_TEXT = {
    "tablets": "Сколько таблеток вы купили?",
    "capsules": "Сколько капсул вы купили?",
    "sachets": "Сколько саше вы купили?",
    "liquid": "Сколько мл вы купили?",
}

# ================== ГЛАВНОЕ МЕНЮ ==================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для учёта лекарств 💊\n\nВыберите действие:",
        reply_markup=main_menu()
    )

# ================== ДОБАВЛЕНИЕ ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

# ================== ТЕКСТ ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_states.get(chat_id)
    if not state:
        return

    text = update.message.text.replace(",", ".").strip()
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
                [InlineKeyboardButton("💧 Жидкая форма", callback_data="form_liquid")],
            ])
        )

    elif state["step"] == "unit_mg":
        data["unit_mg"] = float(text)
        state["step"] = "bought"
        await update.message.reply_text(
            FORM_BUY_TEXT[data["form"]]
        )

    elif state["step"] == "bought":
        data["bought_units"] = float(text)
        state["step"] = "daily_mg"
        await update.message.reply_text("Сколько мг в сутки вы принимаете?")

    elif state["step"] == "daily_mg":
        data["daily_mg"] = float(text)
        state["step"] = "course_type"
        await update.message.reply_text(
            "Выберите длительность приёма:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Дни", callback_data="course_days")],
                [InlineKeyboardButton("Месяцы", callback_data="course_months")],
                [InlineKeyboardButton("Пожизненно", callback_data="course_life")],
            ])
        )

    elif state["step"] == "course_value":
        value = float(text)
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
    data = query.data

    if data == "add":
        await add_start(update, context)

    elif data.startswith("form_"):
        form = data.replace("form_", "")
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"
        await query.message.reply_text(
            f"Сколько мг в одной {FORM_UNIT_MG[form]}?"
        )

    elif data.startswith("course_"):
        if data == "course_life":
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(update, chat_id)
        else:
            user_states[chat_id]["data"]["course_type"] = data.replace("course_", "")
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "summary":
        await summary(update, context)

    elif data == "forecast":
        await forecast(update, context)

    elif data == "delete":
        await delete_menu(update, context)

    elif data.startswith("delete_"):
        name = data.replace("delete_", "")
        data_store.get(chat_id, {}).pop(name, None)
        await query.message.reply_text(
            f"🗑 Лекарство «{name}» удалено",
            reply_markup=main_menu()
        )

# ================== СОХРАНЕНИЕ ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]

    total_mg = data["unit_mg"] * data["bought_units"]
    days_available = total_mg / data["daily_mg"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "total_mg": total_mg,
        "daily_mg": data["daily_mg"],
        "created": datetime.now(),
    }

    msg = (
        f"✅ {data['name']} добавлен.\n"
        f"Хватит на {math.floor(days_available)} дней."
    )

    user_states.pop(chat_id)
    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== СВОДКА ==================

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    if not meds:
        await update.effective_message.reply_text(
            "Список лекарств пуст.",
            reply_markup=main_menu()
        )
        return

    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = int(med["total_mg"] / med["daily_mg"])
        msg += f"{name} — осталось {days} дней\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== ПРОГНОЗ ==================

async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    msg = "⏳ Прогноз:\n\n"
    for name, med in meds.items():
        days = int(med["total_mg"] / med["daily_mg"])
        end_date = med["created"] + timedelta(days=days)
        msg += f"{name} — закончится {end_date.strftime('%d.%m.%Y')}\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ================== УДАЛЕНИЕ ==================

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    if not meds:
        await update.callback_query.message.reply_text(
            "Удалять нечего.",
            reply_markup=main_menu()
        )
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"delete_{name}")]
        for name in meds.keys()
    ]

    await update.callback_query.message.reply_text(
        "Выберите лекарство для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== WEBHOOK ==================

@app.route("/", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "OK"

# ================== РЕГИСТРАЦИЯ ==================

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(buttons))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))


