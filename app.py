import os
import math
import asyncio
from datetime import datetime, timedelta

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
            await update.message.reply_text("Сколько штук купили?")

        elif state["step"] == "bought_units":
            data["bought_units"] = int(text)
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки вы принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("На какой срок приём?", reply_markup=course_keyboard())

        elif state["step"] == "course_value":
            value = int(text)
            data["course_days"] = value * 30 if data["course_type"] == "months" else value
            await save_medicine(update, chat_id)

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            data["unit_mg"] = float(text)
            state["step"] = "units"
            await update.message.reply_text("Сколько штук купили?")

        elif state["step"] == "units":
            units = int(text)
            med = data_store[chat_id][data["name"]]

            added_mg = units * data["unit_mg"]
            med["total_mg"] += added_mg
            med["packs"][data["unit_mg"]] = med["packs"].get(data["unit_mg"], 0) + units

            days = math.floor(med["total_mg"] / med["daily_mg"])

            await update.message.reply_text(
                f"🔄 Пополнение учтено\n"
                f"Добавлено: {int(added_mg)} мг\n"
                f"Хватит на {days} дней",
                reply_markup=main_menu()
            )
            user_states.pop(chat_id)

    elif state["flow"] == "dose":
        med = data_store[chat_id][data["name"]]
        med["daily_mg"] = float(text)

        await update.message.reply_text(
            f"🔧 Дозировка обновлена\n"
            f"{med['daily_mg']} мг/день",
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

    elif data == "summary":
        await show_summary(query)

    elif data == "forecast":
        await show_forecast(query)

    elif data in ("refill", "dose", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Список пуст.", reply_markup=main_menu())
            return

        user_states[chat_id] = {"flow": data, "step": "select", "data": {}}
        keyboard = [[InlineKeyboardButton(name, callback_data=f"{data}_{name}")] for name in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif "_" in data:
        action, name = data.split("_", 1)

        if action == "delete":
            del data_store[chat_id][name]
            await query.message.reply_text(f"🗑 {name} удалено", reply_markup=main_menu())

        elif action == "refill":
            user_states[chat_id]["data"] = {"name": name}
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text("Сколько мг в одной таблетке?")

        elif action == "dose":
            user_states[chat_id]["data"] = {"name": name}
            await query.message.reply_text("Введите новую суточную дозировку (мг):")

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    data = user_states[chat_id]["data"]

    total_mg = data["unit_mg"] * data["bought_units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "form": data["form"],
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "packs": {data["unit_mg"]: data["bought_units"]},
        "course_days": data["course_days"],
        "created": datetime.now(),
        "notified": False,
    }

    await update.effective_message.reply_text(
        f"✅ {data['name']} добавлен",
        reply_markup=main_menu()
    )
    user_states.pop(chat_id)

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "📋 Сводка:\n\n"

    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        msg += f"{name} — {days} дней\n"

    await query.message.reply_text(msg, reply_markup=main_menu())

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"

    for name, med in meds.items():
        days = math.floor(med["total_mg"] / med["daily_mg"])
        end = med["created"] + timedelta(days=days)
        msg += f"{name} — закончится {end.strftime('%d.%m.%Y')}\n"

    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== НАПОМИНАНИЯ ==================

async def reminder_loop(app):
    while True:
        today = datetime.now().date()

        for chat_id, meds in data_store.items():
            for name, med in meds.items():
                days_left = math.floor(med["total_mg"] / med["daily_mg"])

                if days_left == 7 and not med["notified"]:
                    need_mg = (
                        med["daily_mg"] * med["course_days"]
                        if med["course_days"]
                        else med["daily_mg"] * 30
                    ) - med["total_mg"]

                    text = f"⏰ {name} заканчивается через 7 дней\n\nНужно докупить:\n"

                    for unit_mg, count in med["packs"].items():
                        pills = math.ceil(need_mg / unit_mg)
                        text += f"{int(unit_mg)} мг — {pills} шт\n"

                    await app.bot.send_message(chat_id, text)
                    med["notified"] = True

        await asyncio.sleep(86400)

# ================== MAIN ==================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.post_init = lambda app: asyncio.create_task(reminder_loop(app))

    app.run_polling()

if __name__ == "__main__":
    main()



