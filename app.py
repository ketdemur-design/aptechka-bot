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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
started_users = set()
snoozed_reminders = {} # {chat_id: {med_name: next_time}}

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
    "liquid": ("мл", "мл"), # Изменено на мл
    "drops": ("флаконе", "флаконов"),
}

# ================== HELPERS ==================

def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left_in_stock(med):
    """Сколько дней продержится текущий запас"""
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
    text = text.replace("24:00", "00:00")
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.find
