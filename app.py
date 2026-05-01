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
DATA_FILE = Path(os.getenv("DATA_FILE", "meds_data.json"))

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
                if parsed.tzinfo is None:
                    parsed = TZ_MOSCOW.localize(parsed)
                payload[dt_key] = parsed.astimezone(TZ_MOSCOW)
            except:
                payload[dt_key] = None
    payload.setdefault("times", {})
    payload.setdefault("notified", False)
    payload.setdefault("is_started", False)
    payload.setdefault("last_reminder_key", None)
    return payload

def save_data_store():
    serializable = {
        str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
        for chat_id, meds in data_store.items()
    }
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(DATA_FILE)

def load_data_store():
    global data_store
    if not DATA_FILE.exists():
        data_store = {}
        return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data_store.clear()
        for chat_id, meds in raw.items():
            data_store[int(chat_id)] = {name: _deserialize_med(med) for name, med in meds.items()}
    except:
        data_store = {}

# --- ФУНКЦИИ ИНТЕРФЕЙСА ---

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

def reminder_action_menu(med_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Через 20м", callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Через 1 час", callback_data=f"later:60:{med_name}")]
    ])

def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    if not med.get("daily_mg") or med["daily_mg"] <= 0: return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"): return capacity_days
    start_dt = med["start_date"]
    days_passed = (get_now() - start_dt).days
    return max(0, capacity_days - days_passed)

# --- ОБРАБОТЧИКИ БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"Бот аптечка запущен (v{BOT_VERSION})\n\nРаботаю по времени МСК.",
        reply_markup=main_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    if text == "➕ Добавить лекарство":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await update.message.reply_text("Введите название лекарства:")
        return
    elif text == "📋 Мои курсы и прогноз":
        meds = data_store.get(chat_id, {})
        if not meds:
            await update.message.reply_text("Список пуст.")
            return
        res = "📋 Ваши лекарства:\n\n"
        for n, m in meds.items():
            days = calc_days_left(m)
            res += f"💊 {n}: осталось на {days} дн.\n"
        await update.message.reply_text(res)
        return
    
    state = user_states.get(chat_id)
    if not state: return

    d = state.get("data", {})
    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму выпуска:", reply_markup=form_menu())
        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            await update.message.reply_text("Сколько штук/флаконов купили?")
        elif state["step"] == "units":
            d["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько в сутки назначено (мг или мл)?")
        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Длительность курса:", reply_markup=course_menu())
        elif state["step"] == "course_value":
            val = int(float(text.replace(",", ".")))
            if d.get("course_type") == "months": val *= 30
            d["course_days"] = val
            await save_medicine(update, chat_id)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data.startswith("form_"):
        user_states[chat_id]["data"]["form"] = data.split("_")[1]
        user_states[chat_id]["step"] = "unit_mg"
        await query.message.reply_text("Какая дозировка/объем одной единицы?")
    elif data.startswith("course_"):
        ctype = data.split("_"
