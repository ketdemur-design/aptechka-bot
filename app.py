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
    raise RuntimeError("BOT_TOKEN не найден")

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
        "Привет 👋 Я слежу за лекарствами и напомню, когда их пора купить.",
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
            await update.message.reply_text("Сколько единиц купили?")

        elif state["step"] == "bought_units":
            data["bought_units"] = float(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки вы принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_keyboard())

        elif state["step"] == "course_value":
            value = float(text)
            data["course_days"] = value * 30 if data["course_type"] == "months" else value
            await save_medicine(update, chat_id)

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "units"
            await update.message.reply_text("Сколько единиц купили?")

        elif state["step"] == "units":
            units = float(text)
            med = data_store[chat_id][data["name"]]
            added_mg = units * data["unit_mg"]

            med["total_mg"] += added_mg
            med["packages"].append(data["unit_mg"])

            await update.message.reply_text("✅ Пополнение учтено", reply_markup=main_menu())
            user_states.pop(chat_id)

    elif state["flow"] == "dose":
        med = data_store[chat_id][data["name"]]
        med["daily_mg"] = float(text)
        await update.message.reply_text("✅ Дозировка обновлена", reply_markup=main_menu())
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
        state = user_states[chat_id]
        state["data"]["form"] = data.replace("form_", "")
        state["step"] = "unit_mg"
        await query.message.reply_text("Сколько мг в одной единице?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        if data == "course_life":
            state["data"]["course_days"] = None
            await save_medicine(update, chat_id)
        else:
            state["data"]["course_type"] = data.replace("course_", "")
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data in ("refill", "dose", "delete"):
        meds = list(data_store.get(chat_id, {}))
        user_states[chat_id] = {"flow": data, "step": "select", "data": {}}
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}_{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif "_" in data:
        action, name = data.split("_", 1)

        if action == "refill":
            user_states[chat_id]["data"] = {"name": name}
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text("Сколько мг в одной единице?")

        elif action == "dose":
            user_states[chat_id]["data"] = {"name": name}
            await query.message.reply_text("Введите новую дозировку (мг/день):")

        elif action == "delete":
            del data_store[chat_id][name]
            await query.message.reply_text("🗑 Удалено", reply_markup=main_menu())

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]
    total_mg = data["unit_mg"] * data["bought_units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "form": data["form"],
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "packages": [data["unit_mg"]],
        "created": datetime.now(),
        "course_days": data.get("course_days"),
        "notified": False,
    }

    await update.effective_message.reply_text("✅ Лекарство добавлено", reply_markup=main_menu())
    user_states.pop(chat_id)

# ================== НАПОМИНАНИЕ ==================

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, meds in data_store.items():
        for name, med in meds.items():
            days_left = med["total_mg"] / med["daily_mg"]

            if days_left <= 7 and not med["notified"]:
                need_mg = max(0, med["daily_mg"] * 30 - med["total_mg"])
                lines = []

                for dose in sorted(set(med["packages"])):
                    lines.append(f"{int(dose)} мг — {math.ceil(need_mg / dose)} шт")

                await context.bot.send_message(
                    chat_id,
                    f"⚠️ {name} заканчивается через {math.ceil(days_left)} дней\n"
                    f"Нужно докупить:\n" + "\n".join(lines)
                )
                med["notified"] = True

# ================== MAIN ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.job_queue.run_daily(
        reminder_job,
        time=time(hour=10, minute=0)
    )

    app.run_polling()

if __name__ == "__main__":
    main()
