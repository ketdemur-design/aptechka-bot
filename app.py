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

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни", callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет 👋\n"
        "Я помогу тебе учитывать лекарства и вовремя напоминать о покупке 💊\n\n"
        "Что я умею:\n"
        "• считать остаток таблеток\n"
        "• учитывать разные дозировки\n"
        "• пересчитывать остаток таблеток при смене дозировки\n"
        "• напоминать за 7 дней до окончания\n"
        "• не напоминать, если курс лечения закончился\n\n"
        "Нажми кнопку ниже, чтобы начать 👇"
    )
    await update.message.reply_text(text, reply_markup=start_menu())

# ================== ADD ==================

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
            data["unit_mg"] = int(float(text))
            state["step"] = "units"
            await update.message.reply_text("Сколько таблеток купили?")

        elif state["step"] == "units":
            data["units"] = int(float(text))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily_mg":
            data["daily_mg"] = int(float(text))
            state["step"] = "course"
            await update.message.reply_text(
                "Какой курс приёма?",
                reply_markup=course_menu()
            )

        elif state["step"] == "course_value":
            value = int(float(text))
            course_type = data["course_type"]

            if course_type == "days":
                data["course_end"] = datetime.now() + timedelta(days=value)
            elif course_type == "months":
                data["course_end"] = datetime.now() + timedelta(days=value * 30)

            await save_medicine(update, chat_id)
            user_states.pop(chat_id)

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            data["unit_mg"] = int(float(text))
            state["step"] = "units"
            await update.message.reply_text("Сколько таблеток купили?")

        elif state["step"] == "units":
            units = int(float(text))
            med = data_store[chat_id][data["name"]]

            added_mg = units * data["unit_mg"]
            med["total_mg"] += added_mg
            med["purchases"][data["unit_mg"]] = (
                med["purchases"].get(data["unit_mg"], 0) + units
            )
            med["notified"] = False

            days = med["total_mg"] // med["daily_mg"]

            await update.message.reply_text(
                f"🔄 Пополнение учтено\n"
                f"Добавлено: {added_mg} мг\n"
                f"При приёме {med['daily_mg']} мг хватит на {days} дней",
                reply_markup=main_menu()
            )
            user_states.pop(chat_id)

    elif state["flow"] == "dose":
        med = data_store[chat_id][data["name"]]
        med["daily_mg"] = int(float(text))
        med["notified"] = False

        days = med["total_mg"] // med["daily_mg"]
        await update.message.reply_text(
            f"🔧 Дозировка обновлена\nХватит на {days} дней",
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
        await query.message.reply_text(
            "Отлично! Что будем делать?",
            reply_markup=main_menu()
        )

    elif data == "add":
        await add_start(update, context)

    elif data.startswith("course_"):
        state = user_states.get(chat_id)
        if not state:
            return

        course_type = data.split("_")[1]
        info = state["data"]

        if course_type == "forever":
            info["course_end"] = None
            await save_medicine(query, chat_id)
            user_states.pop(chat_id)
            return

        info["course_type"] = course_type
        state["step"] = "course_value"
        await query.message.reply_text(
            "Введите количество дней:" if course_type == "days"
            else "Введите количество месяцев:"
        )

    elif data in ("refill", "dose", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Список пуст.", reply_markup=main_menu())
            return

        user_states[chat_id] = {"flow": data, "step": "select", "data": {}}
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}_{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif "_" in data:
        action, name = data.split("_", 1)

        if action == "delete":
            del data_store[chat_id][name]
            await query.message.reply_text(f"🗑 {name} удалено", reply_markup=main_menu())

        elif action == "refill":
            user_states[chat_id]["data"] = {"name": name}
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text("Сколько мг в таблетке?")

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
    total_mg = data["unit_mg"] * data["units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][data["name"]] = {
        "daily_mg": data["daily_mg"],
        "total_mg": total_mg,
        "created": datetime.now(),
        "purchases": {data["unit_mg"]: data["units"]},
        "course_end": data.get("course_end"),
        "notified": False,
    }

    days = total_mg // data["daily_mg"]

    await update.message.reply_text(
        f"✅ {data['name']} добавлен\nХватит на {days} дней",
        reply_markup=main_menu()
    )

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = med["total_mg"] // med["daily_mg"]
        msg += f"{name} — {days} дней\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

async def show_forecast(query):
    chat_id = query.message.chat.id
    meds = data_store.get(chat_id, {})
    msg = "⏳ Прогноз:\n\n"
    for name, med in meds.items():
        days = med["total_mg"] // med["daily_mg"]
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
                    text += f"• {unit_mg} мг — {pills} таблеток\n"

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
