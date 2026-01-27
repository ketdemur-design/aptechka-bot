import os
import math
from datetime import datetime, timedelta, time

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

REMIND_DAYS = 7

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
            form_plural = FORMS[data["form"]][1]
            await update.message.reply_text(f"Сколько {form_plural} купили?")

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

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "units"
            form_plural = FORMS[data["form"]][1]
            await update.message.reply_text(f"Сколько {form_plural} купили?")

        elif state["step"] == "units":
            units = float(text)
            added_mg = units * data["unit_mg"]
            med = data_store[chat_id][data["name"]]
            med["total_mg"] += added_mg
            med["refills"].append(data["unit_mg"])
            days = math.floor(med["total_mg"] / med["daily_mg"])

            await update.message.reply_text(
                f"🔄 Пополнение учтено\n"
                f"Добавлено: {int(added_mg)} мг\n"
                f"Всего теперь: {int(med['total_mg'])} мг\n"
                f"Хватит на {days} дней",
                reply_markup=main_menu()
            )
            user_states.pop(chat_id)

    elif state["flow"] == "dose":
        med = data_store[chat_id][data["name"]]
        med["daily_mg"] = float(text)
        days = math.floor(med["total_mg"] / med["daily_mg"])

        await update.message.reply_text(
            f"🔧 Дозировка обновлена\n"
            f"Новая дозировка: {med['daily_mg']} мг/день\n"
            f"Хватит на {days} дней",
            reply_markup=main_menu()
        )
        user_states.pop(chat_id)

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
        await query.message.reply_text(
            "Сколько мг в 1 мл?" if form == "liquid" else "Сколько мг в одной таблетке?"
        )

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
