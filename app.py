import os
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

# ================== STORAGE ==================

data_store = {}
user_states = {}
started_users = set()

# ================== CONSTANTS ==================

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
}

# ================== MENUS ==================

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
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

# ================== HELPERS ==================

def days_left(med):
    return int(med["total_mg"] // med["daily_mg"])

def end_date(med):
    if med["course_days"] is None:
        return None
    return med["created"] + timedelta(days=med["course_days"])

def format_status(name, med, with_dates=False):
    days = days_left(med)
    msg = (
        f"💊 {name}\n"
        f"Остаток: {days} дней"
    )

    if with_dates:
        if med["course_days"] is not None:
            end = end_date(med).strftime("%d.%m.%Y")
            msg += f"\nОкончание курса: {end}"
        else:
            msg += "\nКурс: пожизненно ♾"

    return msg

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in started_users:
        await update.message.reply_text(
            "Привет 👋\n\n"
            "Я помогу учитывать лекарства,\n"
            "пересчитывать остатки\n"
            "и напоминать о покупке 💊\n\n"
            "Нажми «Начать» 👇",
            reply_markup=start_menu()
        )

# ================== TEXT HANDLER ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.replace(",", ".").strip()

    if chat_id not in started_users:
        await start(update, context)
        return

    state = user_states.get(chat_id)
    if not state:
        return

    d = state["data"]

    # ---------- ADD FLOW ----------
    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text)
            _, plural = FORM_LABELS[d["form"]]
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            d["units"] = int(float(text))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())

        elif state["step"] == "course_value":
            value = int(float(text))
            if d["course_type"] == "months":
                value *= 30
            d["course_days"] = value
            await save_medicine(update, chat_id)

    # ---------- CHANGE DOSE ----------
    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text)
        med["notified"] = False

        msg = (
            "🔧 Дозировка изменена\n\n"
            f"{format_status(state['medicine'], med)}"
        )

        await update.message.reply_text(msg, reply_markup=main_menu())
        user_states.pop(chat_id)

    # ---------- REFILL ----------
    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text)
            _, plural = FORM_LABELS[state["form"]]
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text))
            med = data_store[chat_id][state["medicine"]]

            med["total_mg"] += units * state["data"]["unit_mg"]
            med["notified"] = False

            msg = (
                "🔄 Лекарство пополнено\n\n"
                f"{format_status(state['medicine'], med)}"
            )

            await update.message.reply_text(msg, reply_markup=main_menu())
            user_states.pop(chat_id)

# ================== BUTTON HANDLER ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "start_bot":
        started_users.add(chat_id)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")

    elif data.startswith("form_"):
        form = data.split("_")[1]
        user_states[chat_id]["data"]["form"] = form
        singular, _ = FORM_LABELS[form]
        user_states[chat_id]["step"] = "unit_mg"
        await query.message.reply_text(f"Сколько мг в одной {singular}?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        if data.endswith("forever"):
            state["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            state["data"]["course_type"] = data.split("_")[1]
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data in ("dose", "refill", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        action, med = data.split(":")

        if action == "dose":
            user_states[chat_id] = {"flow": "dose", "medicine": med, "data": {}}
            await query.message.reply_text("Введите новую суточную дозировку (мг):")

        elif action == "refill":
            form = data_store[chat_id][med]["form"]
            user_states[chat_id] = {
                "flow": "refill",
                "medicine": med,
                "form": form,
                "step": "unit_mg",
                "data": {}
            }
            singular, _ = FORM_LABELS[form]
            await query.message.reply_text(f"Сколько мг в одной {singular}?")

        elif action == "delete":
            data_store[chat_id].pop(med)
            await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

    elif data == "summary":
        meds = data_store.get(chat_id, {})
        msg = "📋 Сводка:\n\n"
        for n, m in meds.items():
            msg += format_status(n, m) + "\n\n"
        await query.message.reply_text(msg.strip(), reply_markup=main_menu())

    elif data == "forecast":
        meds = data_store.get(chat_id, {})
        msg = "⏳ Прогноз:\n\n"
        for n, m in meds.items():
            msg += format_status(n, m, with_dates=True) + "\n\n"
        await query.message.reply_text(msg.strip(), reply_markup=main_menu())

# ================== SAVE MEDICINE ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    total_mg = d["unit_mg"] * d["units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "form": d["form"],
        "unit_mg": d["unit_mg"],
        "daily_mg": d["daily_mg"],
        "total_mg": total_mg,
        "course_days": d.get("course_days"),
        "created": datetime.now(),
        "notified": False,
    }

    med = data_store[chat_id][d["name"]]

    msg = (
        "✅ Лекарство добавлено\n\n"
        f"{format_status(d['name'], med)}"
    )

    await update.message.reply_text(msg, reply_markup=main_menu())
    user_states.pop(chat_id)

# ================== REMINDER ==================

async def reminder_loop(app):
    while True:
        for chat_id, meds in data_store.items():
            for name, med in meds.items():
                if not med["notified"]:
                    d = days_left(med)
                    if 0 < d <= 7:
                        await app.bot.send_message(
                            chat_id,
                            "🛒 Напоминание\n\n"
                            f"{format_status(name, med)}\n"
                            "Пора купить 💊"
                        )
                        med["notified"] = True
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
