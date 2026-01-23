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

BOT_TOKEN = os.getenv("BOT_TOKEN")

data_store = {}
user_states = {}

FORMS_TEXT = {
    "tablets": ("таблетку", "таблеток"),
    "sachets": ("саше", "саше"),
    "capsules": ("капсулу", "капсул"),
    "spoons": ("мг", "мг"),
    "liquid": ("мг", "мг"),
}

# ---------- MENU ----------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

# ---------- START ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Что вы хотите сделать дальше?",
        reply_markup=main_menu()
    )

# ---------- ADD FLOW ----------

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
                [InlineKeyboardButton("Жидкая форма", callback_data="form_liquid")],
            ])
        )

    elif state["step"] == "unit_mg":
        data["unit_mg"] = float(text.replace(",", "."))
        state["step"] = "bought"

        singular, plural = FORMS_TEXT[data["form"]]
        await update.message.reply_text(f"Сколько {plural} вы купили?")

    elif state["step"] == "bought":
        data["bought_units"] = float(text.replace(",", "."))
        state["step"] = "daily_mg"
        await update.message.reply_text("Сколько мг в сутки вам назначено?")

    elif state["step"] == "daily_mg":
        data["daily_mg"] = float(text.replace(",", "."))
        await save_medicine(update, chat_id)

# ---------- BUTTON HANDLER ----------

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

        singular, _ = FORMS_TEXT[form]
        await query.message.reply_text(f"Сколько мг в одной {singular}?")

    elif data == "summary":
        await summary(update, context)

    elif data == "forecast":
        await forecast(update, context)

    elif data == "delete":
        await delete_menu(update, context)

    elif data.startswith("delete_"):
        med_name = data.replace("delete_", "")
        data_store.get(chat_id, {}).pop(med_name, None)
        await query.message.reply_text(
            f"🗑 {med_name} удалено.",
            reply_markup=main_menu()
        )

# ---------- SAVE ----------

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]

    total_mg = data["unit_mg"] * data["bought_units"]
    days_available = total_mg / data["daily_mg"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "name": data["name"],
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "created": datetime.now(),
    }

    msg = f"✅ {data['name']} добавлен.\nХватит на {math.floor(days_available)} дней."

    user_states.pop(chat_id)
    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ---------- DELETE ----------

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})

    if not meds:
        await query.message.reply_text("Удалять нечего.", reply_markup=main_menu())
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"delete_{name}")]
        for name in meds.keys()
    ]

    await query.message.reply_text(
        "Выберите лекарство для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------- SUMMARY ----------

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})

    if not meds:
        await update.effective_message.reply_text("Список пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка:\n\n"
    for med in meds.values():
        days = int(med["total_mg"] / med["daily_mg"])
        msg += f"{med['name']} — осталось {days} дней\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ---------- FORECAST ----------

async def forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"

    for med in meds.values():
        days = int(med["total_mg"] / med["daily_mg"])
        end_date = med["created"] + timedelta(days=days)
        msg += f"{med['name']} — закончится {end_date.strftime('%d.%m.%Y')}\n"

    await update.effective_message.reply_text(msg, reply_markup=main_menu())

# ---------- RUN ----------

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.run_polling()

if __name__ == "__main__":
    main()





