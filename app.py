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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

data_store = {}
user_states = {}
started_users = set()

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

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋\n\n"
        "Я помогу:\n"
        "• следить за лекарствами 💊\n"
        "• считать остаток\n"
        "• напоминать за 7 дней до окончания\n\n"
        "Нажми «Начать», чтобы запустить бота 👇",
        reply_markup=start_menu()
    )

# ================== ADD ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

# ================== TEXT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.replace(",", ".").strip()

    state = user_states.get(chat_id)
    if not state:
        return

    data = state["data"]

    # -------- ДОБАВЛЕНИЕ --------
    if state["flow"] == "add":

        if state["step"] == "name":
            data["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "units"
            await update.message.reply_text("Сколько штук купили?")

        elif state["step"] == "units":
            data["units"] = float(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма?", reply_markup=course_menu())

        elif state["step"] == "course_value":
            value = float(text)
            if data["course_type"] == "days":
                data["course_end"] = datetime.now() + timedelta(days=value)
            else:
                data["course_end"] = datetime.now() + timedelta(days=value * 30)

            await save_medicine(update, chat_id)
            user_states.pop(chat_id)

    # -------- ИЗМЕНЕНИЕ ДОЗИРОВКИ --------
    elif state["flow"] == "dose":

        med = state["medicine"]
        data_store[chat_id][med]["daily_mg"] = float(text)
        data_store[chat_id][med]["notified"] = False

        await update.message.reply_text(
            f"✅ Дозировка для «{med}» обновлена",
            reply_markup=main_menu()
        )
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
        user_states[chat_id]["data"]["form"] = data.split("_")[1]
        user_states[chat_id]["step"] = "unit_mg"
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
            await query.message.reply_text("Введите количество:")

    elif data == "dose":
        meds = data_store.get(chat_id)
        if not meds:
            await query.message.reply_text("Лекарств пока нет")
            return

        med = list(meds.keys())[0]
        user_states[chat_id] = {
            "flow": "dose",
            "medicine": med
        }
        await query.message.reply_text("Введите новую суточную дозировку (мг):")

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
        "daily_mg": d["daily_mg"],
        "total_mg": total_mg,
        "created": datetime.now(),
        "course_end": d.get("course_end"),
        "notified": False,
    }

    await update.message.reply_text(
        f"✅ {d['name']} добавлен",
        reply_markup=main_menu()
    )

# ================== SUMMARY ==================

async def show_summary(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "📋 Сводка:\n\n"

    for name, m in meds.items():
        days_total = m["total_mg"] / m["daily_mg"]
        days_passed = (datetime.now() - m["created"]).days
        days_left = max(0, int(days_total - days_passed))
        msg += f"{name} — осталось {days_left} дней\n"

    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== FORECAST ==================

async def show_forecast(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "⏳ Прогноз:\n\n"

    for name, m in meds.items():
        days_total = m["total_mg"] / m["daily_mg"]
        end = m["created"] + timedelta(days=days_total)
        msg += f"{name} — до {end.strftime('%d.%m.%Y')}\n"

    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== REMINDER ==================

async def reminder_loop(app):
    while True:
        now = datetime.now()

        for chat_id, meds in data_store.items():
            for name, m in meds.items():

                if m["notified"]:
                    continue

                days_total = m["total_mg"] / m["daily_mg"]
                days_passed = (now - m["created"]).days
                days_left = days_total - days_passed

                if 0 < days_left <= 7:
                    await app.bot.send_message(
                        chat_id,
                        f"🛒 Заканчивается {name}\n"
                        f"Осталось ~{int(days_left)} дней.\n"
                        f"Пора купить 💊"
                    )
                    m["notified"] = True

        await asyncio.sleep(86400)

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
