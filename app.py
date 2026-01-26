import os
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

data_store = {}
user_states = {}

FORMS = {
    "tablets": "таблетки",
    "capsules": "капсулы",
    "sachets": "саше",
    "liquid": "мл",
}

# ---------- MENU ----------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

# ---------- START ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для учёта лекарств 💊",
        reply_markup=main_menu()
    )

# ---------- ADD MED ----------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_states[chat_id] = {"step": "name", "data": {}}
    await update.callback_query.message.reply_text("Введите название лекарства:")

# ---------- TEXT HANDLER ----------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_states.get(chat_id)
    if not state:
        return

    text = update.message.text.replace(",", ".").strip()
    data = state.get("data")

    # --- ADD FLOW ---
    if state["step"] == "name":
        data["name"] = text
        state["step"] = "form"
        await update.message.reply_text(
            "Выберите форму:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Таблетки", callback_data="form_tablets")],
                [InlineKeyboardButton("Капсулы", callback_data="form_capsules")],
                [InlineKeyboardButton("Саше", callback_data="form_sachets")],
                [InlineKeyboardButton("Жидкая форма", callback_data="form_liquid")],
            ])
        )

    elif state["step"] == "unit_mg":
        data["unit_mg"] = float(text)
        state["step"] = "bought"
        await update.message.reply_text(
            f"Сколько {FORMS[data['form']]} купили?"
        )

    elif state["step"] == "bought":
        units = float(text)
        data["total_mg"] = units * data["unit_mg"]
        state["step"] = "daily_mg"
        await update.message.reply_text("Сколько мг в сутки назначено?")

    elif state["step"] == "daily_mg":
        data["daily_mg"] = float(text)
        state["step"] = "course"
        await update.message.reply_text(
            "Срок приёма:",
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

    # --- REFILL FLOW ---
    elif state["step"] == "refill_unit_mg":
        state["unit_mg"] = float(text)
        state["step"] = "refill_units"
        await update.message.reply_text("Сколько таблеток купили?")

    elif state["step"] == "refill_units":
        units = float(text)
        added_mg = units * state["unit_mg"]

        med = data_store[chat_id][state["med"]]
        med["total_mg"] += added_mg

        days_left = int(med["total_mg"] / med["daily_mg"])
        end_date = datetime.now() + timedelta(days=days_left)

        user_states.pop(chat_id)

        await update.message.reply_text(
            f"✅ Пополнение учтено\n\n"
            f"➕ Добавлено: {added_mg} мг\n"
            f"📦 Всего: {int(med['total_mg'])} мг\n"
            f"⏳ Хватит на: {days_left} дней\n"
            f"📅 Закончится: {end_date.strftime('%d.%m.%Y')}",
            reply_markup=main_menu()
        )

# ---------- BUTTONS ----------

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    action = query.data

    if action == "add":
        await add_start(update, context)

    elif action.startswith("form_"):
        form = action.replace("form_", "")
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"
        await query.message.reply_text("Сколько мг в одной таблетке?")

    elif action.startswith("course_"):
        state = user_states[chat_id]
        if action == "course_life":
            state["data"]["course_days"] = None
            await save_medicine(update, chat_id)
        else:
            state["data"]["course_type"] = action.replace("course_", "")
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif action == "refill":
        meds = data_store.get(chat_id, {})
        keyboard = [[InlineKeyboardButton(m, callback_data=f"refill_{m}")] for m in meds]
        await query.message.reply_text("Что пополнить?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action.startswith("refill_"):
        med = action.replace("refill_", "")
        user_states[chat_id] = {"step": "refill_unit_mg", "med": med}
        await query.message.reply_text("Сколько мг в одной таблетке?")

    elif action == "summary":
        await summary(update, context)

    elif action == "forecast":
        await forecast(update, context)

    elif action == "delete":
        meds = data_store.get(chat_id, {})
        keyboard = [[InlineKeyboardButton(m, callback_data=f"delete_{m}")] for m in meds]
        await query.message.reply_text("Что удалить?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action.startswith("delete_"):
        med = action.replace("delete_", "")
        data_store[chat_id].pop(med)
        await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

# ---------- SAVE ----------

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]
    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        **data,
        "created": datetime.now()
    }
    user_states.pop(chat_id)
    await update.effective_message.reply_text(
        "✅ Лекарство добавлено",
        reply_markup=main_menu()
    )

# ---------- SUMMARY ----------

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    if not meds:
        await update.effective_message.reply_text("Список пуст", reply_markup=main_menu())
        return

    msg = "📋 Сводка:\n\n"
    for med in meds.values():
        days = int(med["total_mg"] / med["daily_mg"])
        msg += f"{med['name']} — {days} дней\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ---------- FORECAST ----------

async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"
    for med in meds.values():
        days = int(med["total_mg"] / med["daily_mg"])
        date = datetime.now() + timedelta(days=days)
        msg += f"{med['name']} — {date.strftime('%d.%m.%Y')}\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ---------- RUN ----------

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
