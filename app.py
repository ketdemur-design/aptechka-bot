import os 
import asyncio
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
import pytz
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Application
)

# --- КОНФИГУРАЦИЯ ---
TZ_MOSCOW = pytz.timezone('Europe/Moscow')
BOT_VERSION = "1.1.26"
DATA_FILE = Path(os.getenv("DATA_FILE", "/data/meds_data.json"))

data_store = {}
user_states = {}
started_users = set()

DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

# --- ФУНКЦИИ ДАННЫХ ---
def _serialize_med(med):
    payload = dict(med)
    for dt_key in ("created", "start_date"):
        if isinstance(payload.get(dt_key), datetime):
            payload[dt_key] = payload[dt_key].isoformat()
    return payload

def _deserialize_med(med):
    payload = dict(med)
    for dt_key in ("created", "start_date"):
        value = payload.get(dt_key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None: parsed = TZ_MOSCOW.localize(parsed)
                payload[dt_key] = parsed.astimezone(TZ_MOSCOW)
            except: payload[dt_key] = None
    payload.setdefault("times", {})
    payload.setdefault("notified", False)
    payload.setdefault("is_started", False)
    payload.setdefault("last_reminder_key", None)
    return payload

def save_data_store():
    serializable = {str(cid): {n: _serialize_med(m) for n, m in meds.items()} for cid, meds in data_store.items()}
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(DATA_FILE)

def load_data_store():
    global data_store
    if not DATA_FILE.exists(): return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data_store.clear()
        for cid, meds in raw.items():
            data_store[int(cid)] = {n: _deserialize_med(m) for n, m in meds.items()}
    except: pass

# --- ИНТЕРФЕЙС ---
def main_menu():
    return ReplyKeyboardMarkup([
        ["➕ Добавить лекарство", "▶️ Начать курс"],
        ["♻️ Докуплено / Пополнить", "🛠️ Изменить дозировку"],
        ["⏰ Напоминание (Дни/Время)"],
        ["📋 Мои курсы и прогноз"],
        ["🗑 Удалить лекарство"]
    ], resize_keyboard=True)

def form_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets")],
        [InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops")],
        [InlineKeyboardButton("💨 Спрей", callback_data="form_spray")],
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet")],
        [InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
    ])

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни", callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Привет! Бот v{BOT_VERSION} готов к работе.", reply_markup=main_menu())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text == "➕ Добавить лекарство":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await update.message.reply_text("Назовите лекарство:")
        return
    state = user_states.get(chat_id)
    if state and state["flow"] == "add":
        # Логика добавления...
        pass

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data
    if data.startswith("form_"):
        user_states[chat_id]["data"]["form"] = data.split("_")[1]
        user_states[chat_id]["step"] = "unit_mg"
        await query.message.reply_text("Введите дозировку/объем одной ед.:")
    elif data.startswith("course_"):
        ctype = data.split("_")[1]
        user_states[chat_id]["data"]["course_type"] = ctype
        user_states[chat_id]["step"] = "course_value"
        await query.message.reply_text("Введите количество:")

# --- ЗАПУСК ---
async def reminder_loop(application: Application):
    while True:
        await asyncio.sleep(60)

async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "перезапустить бота")])
    asyncio.create_task(reminder_loop(application))

def main():
    load_data_store()
    token = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
