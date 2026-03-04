import os
import asyncio
import re
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

BOT_VERSION = "1.2.0"  # <-- ВЕРСИЯ БОТА
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
started_users = set()
snoozed_reminders = {} # {chat_id: {med_name: next_time_str}}

DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("мл", "мл"), 
    "drops": ("флаконе", "флаконов"),
}

# ================== HELPERS ==================

def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left_in_stock(med):
    """Сколько дней продержится текущий запас"""
    if med["daily_mg"] <= 0: return 0
    return int(med["total_mg"] // med["daily_mg"])

def calc_days_left_in_course(med):
    """Сколько дней осталось пить по плану курса"""
    if not med.get("course_days"):
        return float('inf')
    if not med.get("is_started") or not med.get("start_date"):
        return med["course_days"]
    
    start_dt = med["start_date"]
    now_dt = get_now()
    days_passed = (now_dt - start_dt).days
    left = med["course_days"] - days_passed
    return max(0, left)

def parse_times(text):
    # Исправление для 24:00
    text = text.replace("24:00", "00:00")
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if hh == 24: hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

def format_schedule(times_dict):
    if not isinstance(times_dict, dict) or not times_dict:
        return "не установлено"
    lines = []
    # Порядок вывода в тексте
    order = ["Everyday", "Weekdays", "Weekends", "0", "1", "2", "3", "4", "5", "6"]
    for key in order:
        if key in times_dict and times_dict[key]:
            lines.append(f"{DAYS_MAP[key]}: {', '.join(times_dict[key])}")
    return "\n".join(lines) if lines else "не установлено"

def get_display_units(med):
    form = med.get("form", "tablets")
    if form in ["liquid", "drops"]:
        return "мл", "мл" if form == "liquid" else "капель"
    return "мг", "мг"

# ================== МЕНЮ ==================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить", callback_data="add"), InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("▶️ Начать курс", callback_data="start_course"), InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🔄 Пополнить", callback_data="refill"), InlineKeyboardButton("🔧 Доза", callback_data="dose")],
        [InlineKeyboardButton("⏰ Расписание", callback_data="reminder_menu"), InlineKeyboardButton("🗑 Удалить", callback_data="delete")],
    ])

def days_menu(med_name, times_dict=None):
    if not isinstance(times_dict, dict): times_dict = {}
    keyboard = []
    
    # Спец-группы
    for key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"⭐ {DAYS_MAP[key]}"
        if times_dict.get(key): text += f" ({len(times_dict[key])} р/д)"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{key}")])
    
    # Дни недели по сетке 4+3
    row = []
    for i in range(7):
        day_key = str(i)
        text = DAYS_MAP[day_key][:2] 
        if times_dict.get(day_key): text += " ✅"
        row.append(InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{day_key}"))
        if len(row) == 4 or i == 6:
            keyboard.append(row)
            row = []

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def reminder_keyboard(med_name):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил(а)", callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏳ Через 20 мин", callback_data=f"snooze:{med_name}")]
    ])

# ================== ОБРАБОТЧИКИ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"💊 Бот-аптечка (v{BOT_VERSION})\n\n"
        "Я помогу контролировать прием лекарств и остатки в упаковках.\n"
        "Работаю по времени МСК.",
        reply_markup=main_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    state = user_states.get(chat_id)
    if not state: return

    # FLOW: ADD MEDICINE
    if state["flow"] == "add":
        if state["step"] == "name":
            state["data"]["name"] = text
            state["step"] = "form"
