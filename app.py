import os
import math
from datetime import datetime

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

BOT_TOKEN = os.getenv("BOT_TOKEN")

# ================== ХРАНИЛИЩА ==================

data_store = {}      # chat_id -> medicines
user_states = {}     # chat_id -> state

# ================== НАСТРОЙКИ ==================

REMINDER_DAYS = 7
LIFE_COURSE_DAYS = 30  # расчёт на 30 дней для пожизненного курса

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

# ================== КЛАВИАТУРЫ ==================

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

    # ---------- ADD ----------
    if state["flow"] == "add":

        if state["step"] == "name":
            data["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_keyboard())

        elif state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "bought_units"
            await update.message.reply_text("Сколько таблеток купили?")

        elif state["step"] == "bought_units":
            data["bought_units"] = float(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки вы принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("На какой срок приём?", reply_markup=course_keyboard())

        elif state["step"] == "course_value":
            value = int(float(text))
            data["course_days"] = value * 30 if data["course_type"] == "months" else value
            await save_medicine(update, chat_id)

    # ---------- REFILL ----------
    elif state["flow"] == "refill":

        if state["step"] == "unit_mg":
            data["unit_mg_new"] = float(text)
            state["step"] = "units"
            await update.message.reply_text("Сколько таблеток купили?")

        elif state["step"] == "units":
            units = float(text)
            added_mg = units * data["unit_mg_new"]

            data["total_mg"] += added_mg
            data["unit_mg_variants"].add(data["unit_mg_new"])
            data["notified"] = False

            days = math.floor(data["total_mg"] / data["daily_mg"])

            await update.message.reply_text(
                f"🔄 Пополнение учтено\n"
                f"При приёме {int(data['daily_mg'])} мг хватит примерно на {days} дней",
                reply_markup=main_menu()
            )
            user_states.pop(chat_id)

    # ---------- CHANGE DOSE ----------
    elif state["flow"] == "dose":
        med = data_store[chat_id][data["name"]]
        med["daily_mg"] = float(text)
        med["notified"] = False

        await update.message.reply_text(
            "🔧 Дозировка обновлена",
            reply_markup=main_menu()
        )
        user_states.pop(chat_id)

# ================== BUTTON HANDLER ==================

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

    elif data in ("refill", "dose", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Список лекарств пуст", reply_markup=main_menu())
            return

        keyboard = [[InlineKeyboardButton(m, callback_data=f"{data}_{m}")] for m in meds]
        user_states[chat_id] = {"flow": data, "step": "select", "data": {}}
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif "_" in data:
        action, name = data.split("_", 1)
        med = data_store[chat_id][name]

        if action == "delete":
            del data_store[chat_id][name]
            await query.message.reply_text(f"🗑 {name} удалено", reply_markup=main_menu())

        elif action == "refill":
            user_states[chat_id]["data"] = med
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text("Сколько мг в одной таблетке?")

        elif action == "dose":
            user_states[chat_id]["data"] = {"name": name}
            await query.message.reply_text("Введите новую суточную дозировку (мг):")

    elif data == "summary":
        await show_summary(query)

    elif data == "forecast":
        await show_forecast(query)

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]
    total_mg = data["unit_mg"] * data["bought_units"]
    days = math.floor(total_mg / data["daily_mg"])

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "course_days": data.get("course_days"),
        "created": datetime.now(),
        "unit_mg_variants": {data["unit_mg"]},
        "notified": False,
    }

    await update.effective_message.reply_text(
        f"✅ {data['name']} добавлен\n"
        f"Хватит примерно на {days} дней",
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

    text = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        text += f"• {name}: {days} дней\n"

    await query.message.reply_text(text, reply_markup=main_menu())

# ================== FORECAST ==================

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    text = "⏳ Прогноз:\n\n"

    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        text += f"• {name} закончится через {days} дней\n"

    await query.message.reply_text(text, reply_markup=main_menu())

# ================== УВЕДОМЛЕНИЯ ==================

async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, meds in data_store.items():
        for name, med in meds.items():

            daily = med["daily_mg"]
            if daily <= 0:
                continue

            days_left = math.floor(med["total_mg"] / daily)

            if days_left > REMINDER_DAYS:
                med["notified"] = False
                continue

            target_days = LIFE_COURSE_DAYS if med["course_days"] is None else med["course_days"]
            needed_mg = max(0, daily * target_days - med["total_mg"])

            if needed_mg <= 0 or med["notified"]:
                continue

            msg = (
                f"⚠️ {name} заканчивается\n"
                f"Осталось примерно {days_left} дней\n\n"
                f"Рекомендуется докупить:\n"
            )

            for unit in sorted(med["unit_mg_variants"]):
                tablets = math.ceil(needed_mg / unit)
                msg += f"• {int(unit)} мг — {tablets} шт\n"

            if med["course_days"] is None:
                msg += "\n(пожизненный приём, расчёт на 30 дней)"

            await context.bot.send_message(chat_id, msg)
            med["notified"] = True

# ================== MAIN ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.job_queue.run_repeating(daily_check, interval=86400, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()

