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
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]])

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

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
}

# ================== HELPERS ==================

def calc_days_left(med):
    return int(med["total_mg"] // med["daily_mg"])

def calc_surplus(med):
    if med["course_days"] is None:
        return None
    needed_mg = med["course_days"] * med["daily_mg"]
    surplus_mg = med["total_mg"] - needed_mg
    if surplus_mg <= 0:
        return None
    units = int(surplus_mg // med["unit_mg"])
    days = int(surplus_mg // med["daily_mg"])
    return units, days

def format_med_block(name, med, with_dates=False):
    days = calc_days_left(med)
    surplus = calc_surplus(med)

    msg = (
        f"Название: {name}\n"
        f"Хватит на: {days} дней"
    )

    if med["course_days"]:
        if with_dates:
            end_date = med["created"] + timedelta(days=med["course_days"])
            msg += f"\nДлительность курса: {med['course_days']} дней до {end_date.strftime('%d.%m.%Y')}"
        else:
            msg += f"\nДлительность курса: {med['course_days']} дней"

    if surplus:
        u, d = surplus
        if with_dates:
            surplus_date = datetime.now() + timedelta(days=d)
            msg += f"\nИзлишек: {u} ед. — на {d} дней до {surplus_date.strftime('%d.%m.%Y')}"
        else:
            msg += f"\nИзлишек: {u} ед. — на {d} дней"

    return msg

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

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text)
            state["step"] = "units"
            _, plural = FORM_LABELS[state["form"]]
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text))
            med = data_store[chat_id][state["medicine"]]

            added_mg = units * state["data"]["unit_mg"]
            med["total_mg"] += added_mg
            med["notified"] = False

            msg = "🔄 Лекарство пополнено\n\n" + format_med_block(state["medicine"], med)
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

    elif data == "summary":
        meds = data_store.get(chat_id, {})
        msg = "📋 Сводка:\n\n"
        for n, m in meds.items():
            msg += format_med_block(n, m) + "\n\n"
        await query.message.reply_text(msg, reply_markup=main_menu())

    elif data == "forecast":
        meds = data_store.get(chat_id, {})
        msg = "⏳ Прогноз:\n\n"
        for n, m in meds.items():
            days = calc_days_left(m)
            status = "на курс хватит, докупать не нужно" if not m["course_days"] or days >= m["course_days"] else "на курс не хватит, нужно докупить"
            msg += format_med_block(n, m, with_dates=True) + f"\n{status}\n\n"
        await query.message.reply_text(msg, reply_markup=main_menu())

    elif data in ("add", "dose", "refill", "delete"):
        if data == "add":
            user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
            await query.message.reply_text("Введите название лекарства:")
            return

        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return

        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        action, med = data.split(":")
        if action == "refill":
            form = data_store[chat_id][med].get("form", "tablets")
            user_states[chat_id] = {
                "flow": "refill",
                "medicine": med,
                "step": "unit_mg",
                "form": form,
                "data": {}
            }
            await query.message.reply_text("Сколько мг в одной единице?")

        elif action == "delete":
            data_store[chat_id].pop(med)
            await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    total_mg = d["unit_mg"] * d["units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "daily_mg": d["daily_mg"],
        "unit_mg": d["unit_mg"],
        "total_mg": total_mg,
        "course_days": d.get("course_days"),
        "created": datetime.now(),
        "notified": False,
    }

    med = data_store[chat_id][d["name"]]
    msg = "✅ Лекарство добавлено\n\n" + format_med_block(d["name"], med)

    await update.message.reply_text(msg, reply_markup=main_menu())
    user_states.pop(chat_id)

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

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
