import os
import math
import asyncio
from datetime import datetime

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

# ================== ХРАНИЛИЩА ==================

data_store = {}
user_states = {}

# ================== НАСТРОЙКИ ==================

REMINDER_DAYS = 7
LIFE_COURSE_DAYS = 30
CHECK_INTERVAL = 86400  # 24 часа

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
        "Привет 👋\nЯ помогу учитывать лекарства.",
        reply_markup=main_menu()
    )

# ================== КЛАВИАТУРЫ ==================

def form_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Таблетки", callback_data="form_tablets")],
        [InlineKeyboardButton("Капсулы", callback_data="form_capsules")],
    ])

def course_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Дни", callback_data="course_days")],
        [InlineKeyboardButton("Месяцы", callback_data="course_months")],
        [InlineKeyboardButton("Пожизненно", callback_data="course_life")],
    ])

# ================== ADD FLOW ==================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

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
            state["step"] = "unit_mg"
            await update.message.reply_text("Сколько мг в одной таблетке?")

        elif state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "bought"
            await update.message.reply_text("Сколько таблеток купили?")

        elif state["step"] == "bought":
            data["bought"] = float(text)
            state["step"] = "daily"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily":
            data["daily"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_keyboard())

        elif state["step"] == "course_value":
            value = int(float(text))
            data["course_days"] = value * 30 if data["course_type"] == "months" else value
            await save_med(update, chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    d = query.data

    if d == "add":
        await add_start(update, context)

    elif d.startswith("course_"):
        state = user_states[chat_id]
        if d == "course_life":
            state["data"]["course_days"] = None
            await save_med(update, chat_id)
        else:
            state["data"]["course_type"] = d.replace("course_", "")
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif d == "summary":
        await show_summary(query)

    elif d == "forecast":
        await show_forecast(query)

# ================== SAVE ==================

async def save_med(update, chat_id):
    d = user_states[chat_id]["data"]
    total_mg = d["unit_mg"] * d["bought"]
    days = math.floor(total_mg / d["daily"])

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "daily": d["daily"],
        "total_mg": total_mg,
        "course_days": d.get("course_days"),
        "units": {d["unit_mg"]},
        "notified": False,
    }

    await update.effective_message.reply_text(
        f"✅ {d['name']} добавлен\nХватит примерно на {days} дней",
        reply_markup=main_menu()
    )
    user_states.pop(chat_id)

# ================== SUMMARY ==================

async def show_summary(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    if not meds:
        await query.message.reply_text("Список пуст", reply_markup=main_menu())
        return

    text = "📋 Сводка:\n"
    for name, m in meds.items():
        days = math.floor(m["total_mg"] / m["daily"])
        text += f"• {name}: {days} дней\n"

    await query.message.reply_text(text, reply_markup=main_menu())

# ================== FORECAST ==================

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    text = "⏳ Прогноз:\n"
    for name, m in meds.items():
        days = math.floor(m["total_mg"] / m["daily"])
        text += f"• {name}: {days} дней\n"
    await query.message.reply_text(text, reply_markup=main_menu())

# ================== ФОНОВЫЕ УВЕДОМЛЕНИЯ ==================

async def reminder_loop(app):
    while True:
        for chat_id, meds in data_store.items():
            for name, m in meds.items():
                days_left = math.floor(m["total_mg"] / m["daily"])

                if days_left > REMINDER_DAYS:
                    m["notified"] = False
                    continue

                if m["notified"]:
                    continue

                target = LIFE_COURSE_DAYS if m["course_days"] is None else m["course_days"]
                needed_mg = max(0, m["daily"] * target - m["total_mg"])

                if needed_mg <= 0:
                    continue

                msg = (
                    f"⚠️ {name} заканчивается\n"
                    f"Осталось {days_left} дней\n\n"
                    f"Рекомендуется докупить:\n"
                )

                for unit in sorted(m["units"]):
                    count = math.ceil(needed_mg / unit)
                    msg += f"• {int(unit)} мг — {count} таблеток\n"

                if m["course_days"] is None:
                    msg += "\n(пожизненный приём, расчёт на 30 дней)"

                await app.bot.send_message(chat_id, msg)
                m["notified"] = True

        await asyncio.sleep(CHECK_INTERVAL)

# ================== MAIN ==================

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    asyncio.create_task(reminder_loop(app))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())


