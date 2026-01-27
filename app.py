import os
import math
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

# ================== TOKEN ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

# ================== ХРАНИЛИЩА ==================

data_store = {}      # chat_id -> medicines
user_states = {}     # chat_id -> state
started_users = set()  # кто уже нажал "Начать"

# ================== МЕНЮ ==================

def start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]
    ])

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

FORM_LABELS = {
    "tablets": "таблеток",
    "capsules": "капсул",
    "sachet": "саше",
    "liquid": "мл",
}

# ================== ПРИВЕТСТВИЕ ==================

WELCOME_TEXT = (
    "Привет 👋\n"
    "Я помогу тебе учитывать лекарства и вовремя напоминать о покупке 💊\n\n"
    "Что я умею:\n"
    "• считаю остаток лекарств\n"
    "• учитываю разные дозировки\n"
    "• пересчитываю всё при смене дозы\n"
    "• напоминаю за 7 дней до окончания\n"
    "• не беспокою, если курс лечения завершён\n\n"
    "Нажми кнопку ниже, чтобы начать 👇"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=start_menu())

# ================== ADD ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

# ================== TEXT HANDLER ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # ⬇️ если человек просто написал что угодно, но не запускал бота
    if chat_id not in started_users and chat_id not in user_states:
        await update.message.reply_text(WELCOME_TEXT, reply_markup=start_menu())
        return

    state = user_states.get(chat_id)
    if not state:
        return

    text = update.message.text.replace(",", ".").strip()
    data = state["data"]

    if state["flow"] == "add":

        if state["step"] == "name":
            data["name"] = text
            state["step"] = "form"
            await update.message.reply_text(
                "Выберите форму лекарства:",
                reply_markup=form_menu()
            )

        elif state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "units"
            label = FORM_LABELS[data["form"]]
            await update.message.reply_text(f"Сколько {label} купили?")

        elif state["step"] == "units":
            data["units"] = float(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text(
                "Какой срок приёма?",
                reply_markup=course_menu()
            )

        elif state["step"] == "course_value":
            value = float(text)
            if data["course_type"] == "days":
                data["course_end"] = datetime.now() + timedelta(days=value)
            else:
                data["course_end"] = datetime.now() + timedelta(days=value * 30)

            await save_medicine(update, chat_id)
            user_states.pop(chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "start_bot":
        started_users.add(chat_id)
        await query.message.reply_text("Что будем делать?", reply_markup=main_menu())

    elif data == "add":
        await add_start(update, context)

    elif data.startswith("form_"):
        form = data.split("_")[1]
        state = user_states[chat_id]
        state["data"]["form"] = form
        state["step"] = "unit_mg"
        await query.message.reply_text("Сколько мг в одной единице?")

    elif data.startswith("course_"):
        course = data.split("_")[1]
        state = user_states[chat_id]
        if course == "forever":
            state["data"]["course_end"] = None
            await save_medicine(query, chat_id)
            user_states.pop(chat_id)
        else:
            state["data"]["course_type"] = course
            state["step"] = "course_value"
            await query.message.reply_text(
                "Введите количество дней:" if course == "days"
                else "Введите количество месяцев (можно 1.5):"
            )

    elif data == "summary":
        await show_summary(query)

    elif data == "forecast":
        await show_forecast(query)

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
        "purchases": {d["unit_mg"]: d["units"]},
        "course_end": d.get("course_end"),
        "notified": False,
    }

    await update.message.reply_text(
        f"✅ {d['name']} добавлен",
        reply_markup=main_menu()
    )

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = med["total_mg"] / med["daily_mg"]
        msg += f"{name} — {int(days)} дней\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"
    for name, med in meds.items():
        days = med["total_mg"] / med["daily_mg"]
        end = med["created"] + timedelta(days=days)
        msg += f"{name} — {end.strftime('%d.%m.%Y')}\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== НАПОМИНАНИЯ ==================

async def reminder_loop(app):
    while True:
        now = datetime.now()
        for chat_id, meds in data_store.items():
            for name, med in meds.items():

                if med["notified"]:
                    continue
                if med["course_end"] and now > med["course_end"]:
                    continue

                days_left = med["total_mg"] / med["daily_mg"]
                if days_left > 7:
                    continue

                need_mg = math.ceil((7 - days_left) * med["daily_mg"])
                if need_mg <= 0:
                    continue

                text = f"🛒 Пора купить {name}\n\nНужно докупить {need_mg} мг:\n"
                for unit_mg in med["purchases"]:
                    pills = math.ceil(need_mg / unit_mg)
                    text += f"• {unit_mg} мг — {pills} единиц\n"

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
