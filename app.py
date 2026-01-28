import os
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
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]]
    )

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

# ================== ВСПОМОГАТЕЛЬНОЕ ==================

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
}

def calc_course_days(med):
    if med["course_type"] == "forever":
        return None
    return med["course_days"]

def calc_days_left(med):
    days_total = med["total_mg"] / med["daily_mg"]
    days_passed = (datetime.now() - med["created"]).days
    return max(0, int(days_total - days_passed))

def calc_surplus(med):
    if med["course_type"] == "forever":
        return None

    needed_mg = med["course_days"] * med["daily_mg"]
    surplus_mg = med["total_mg"] - needed_mg

    if surplus_mg <= 0:
        return None

    unit_mg = med["last_unit_mg"]
    units = int(surplus_mg // unit_mg)
    days = int(surplus_mg // med["daily_mg"])

    return units, days

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in started_users:
        await update.message.reply_text(
            "Привет 👋\n\n"
            "Я помогу:\n"
            "• следить за количеством лекарств 💊\n"
            "• учитывать разные дозировки\n"
            "• пересчитывать остатки\n"
            "• напоминать о покупке за 7 дней\n\n"
            "Нажми «Начать», чтобы запустить бота 👇",
            reply_markup=start_menu()
        )

# ================== TEXT ==================

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

    if state["flow"] == "add":

        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму лекарства:", reply_markup=form_menu())

        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text)
            singular, plural = FORM_LABELS[d["form"]]
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
            if d["course_type"] == "days":
                d["course_days"] = value
            else:
                d["course_days"] = value * 30
            await save_medicine(update, chat_id)

    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text)
        med["notified"] = False

        days = calc_days_left(med)
        surplus = calc_surplus(med)

        msg = (
            "🔧 Дозировка изменена\n\n"
            f"Теперь хватает на: {days} дней"
        )

        if surplus:
            units, d_days = surplus
            msg += f"\nИзлишек: {units} ед. — на {d_days} дней"

        await update.message.reply_text(msg, reply_markup=main_menu())
        user_states.pop(chat_id)

# ================== BUTTONS ==================

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
        state = user_states[chat_id]
        state["data"]["form"] = form
        singular, _ = FORM_LABELS[form]
        state["step"] = "unit_mg"
        await query.message.reply_text(f"Сколько мг в одной {singular}?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        if data.endswith("forever"):
            state["data"]["course_type"] = "forever"
            state["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            state["data"]["course_type"] = data.split("_")[1]
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data in ("dose", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств пока нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        action, med = data.split(":")
        if action == "dose":
            user_states[chat_id] = {"flow": "dose", "medicine": med}
            await query.message.reply_text("Введите новую суточную дозировку (мг):")
        elif action == "delete":
            data_store[chat_id].pop(med)
            await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

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
        "course_type": d["course_type"],
        "course_days": d.get("course_days"),
        "last_unit_mg": d["unit_mg"],
        "notified": False,
    }

    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    surplus = calc_surplus(med)

    msg = (
        "✅ Лекарство добавлено\n\n"
        f"Название: {d['name']}\n"
        f"Хватит на: {days} дней (до конца курса)"
    )

    if surplus:
        units, s_days = surplus
        msg += f"\nИзлишек: {units} ед. — на {s_days} дней"

    await update.message.reply_text(msg, reply_markup=main_menu())
    user_states.pop(chat_id)

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "📋 Сводка:\n\n"
    for name, m in meds.items():
        msg += f"{name} — хватит на {calc_days_left(m)} дней\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

async def show_forecast(query):
    meds = data_store.get(query.message.chat.id, {})
    msg = "⏳ Прогноз:\n\n"
    for name, m in meds.items():
        end = m["created"] + timedelta(days=calc_days_left(m))
        msg += f"{name} — до {end.strftime('%d.%m.%Y')}\n"
    await query.message.reply_text(msg, reply_markup=main_menu())

# ================== REMINDER ==================

async def reminder_loop(app):
    while True:
        for chat_id, meds in data_store.items():
            for name, m in meds.items():
                if not m["notified"]:
                    days = calc_days_left(m)
                    if 0 < days <= 7:
                        await app.bot.send_message(
                            chat_id,
                            f"🛒 Заканчивается {name}\n"
                            f"Хватит на: {days} дней\n"
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
