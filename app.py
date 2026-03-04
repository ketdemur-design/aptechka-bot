import os
import asyncio
import re
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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

# Настройки бота
BOT_VERSION = "1.0.5"
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
started_users = set()
snoozed_alerts = {} # Для хранения отложенных напоминаний

DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

# ================== МЕНЮ (ВАШИ ОРИГИНАЛЬНЫЕ) ==================

def start_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]])

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("▶️ Начать курс", callback_data="start_course")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("🔧 Изменить дозировку", callback_data="dose")],
        [InlineKeyboardButton("⏰ Напоминание (Дни/Время)", callback_data="reminder_menu")],
        [InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🗑 Удалить лекарство", callback_data="delete")],
    ])

def form_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets")],
        [InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops")],
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet")],
        [InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
    ])

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни", callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

def days_menu(med_name, times_dict=None):
    if not isinstance(times_dict, dict):
        times_dict = {}
    keyboard = []

    # Добавлены кнопки групп
    for key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"📅 {DAYS_MAP[key]}"
        if key in times_dict and times_dict[key]:
            text += f" ({', '.join(times_dict[key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{key}")])

    # Кнопки дней недели
    for i in range(7):
        day_key = str(i)
        button_text = DAYS_MAP[day_key]
        if day_key in times_dict and times_dict[day_key]:
            button_text += f" ({', '.join(times_dict[day_key])})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_day:{med_name}:{day_key}")])

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

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

def calc_days_left(med):
    if med["daily_mg"] <= 0: return 0
    return int(med["total_mg"] // med["daily_mg"])

def calc_course_remains(med):
    """Считает сколько дней осталось принимать лекарство по курсу"""
    if not med.get("course_days"): return float('inf')
    if not med.get("is_started") or not med.get("start_date"): return med["course_days"]
    days_passed = (get_now() - med["start_date"]).days
    remains = med["course_days"] - days_passed
    return max(0, remains)

def parse_times(text):
    text = text.replace("24:00", "00:00") # Исправление 24:00
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
    if not isinstance(times_dict, dict): return "не установлено"
    lines = []
    for k, v in times_dict.items():
        if v: lines.append(f"{DAYS_MAP.get(k, k)}: {', '.join(v)}")
    return "\n".join(lines) if lines else "не установлено"

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops": return "мл", "капель"
    if form == "liquid": return "мл", "мл" # Для жидких мл
    return "мг", "мг"

# ================== HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"Привет 👋 (Версия {BOT_VERSION})\n\n"
        "Я помогу:\n"
        "• следить за остатками лекарств 💊\n"
        "• напоминать о приеме по времени ⏰\n\n"
        "Нажми «Начать», чтобы запустить меню 👇",
        reply_markup=start_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    state = user_states.get(chat_id)
    if not state: return

    d = state.get("data", {})

    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())
        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            _, plural = FORM_LABELS.get(d["form"], ("ед.", "ед."))
            await update.message.reply_text(f"Сколько {plural} купили?")
        elif state["step"] == "units":
            d["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
            label = "мл" if d.get("form") == "liquid" else "мг"
            if d.get("form") == "drops": label = "капель"
            await update.message.reply_text(f"Сколько {label} в сутки назначено?")
        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())
        elif state["step"] == "course_value":
            val = int(float(text.replace(",", ".")))
            if d["course_type"] == "months": val *= 30
            d["course_days"] = val
            await save_medicine(update, chat_id)

    elif state["flow"] == "set_reminder":
        med_name, day_key = state["medicine"], state["day_key"]
        med_data = data_store[chat_id][med_name]
        if text.lower() in ["0", "удалить"]:
            med_data["times"].pop(day_key, None)
            msg = "🗑 Удалено."
        else:
            times = parse_times(text)
            if not times:
                await update.message.reply_text("⚠️ Неверный формат.")
                return
            med_data["times"][day_key] = times
            msg = f"✅ Сохранено для {DAYS_MAP.get(day_key, day_key)}"
        user_states.pop(chat_id)
        await update.message.reply_text(msg, reply_markup=days_menu(med_name, med_data["times"]))

    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text.replace(",", "."))
        await update.message.reply_text("🔧 Дозировка изменена", reply_markup=main_menu())
        user_states.pop(chat_id)

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            await update.message.reply_text("Сколько купили?")
        elif state["step"] == "units":
            units = int(float(text.replace(",", ".")))
            med = data_store[chat_id][state["medicine"]]
            added = state["data"]["unit_mg"] * units
            if med.get("form") == "drops": added = (state["data"]["unit_mg"] / 0.05) * units
            med["total_mg"] += added
            med["notified"] = False
            await update.message.reply_text("🔄 Пополнено", reply_markup=main_menu())
            user_states.pop(chat_id)

# ================== CALLBACKS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "start_bot" or data == "main_menu":
        user_states.pop(chat_id, None)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")

    elif data.startswith("form_"):
        form = data.split("_")[1]
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"
        label = "Объем флакона (мл)?" if form in ["drops", "liquid"] else "Сколько мг в одной ед.?"
        await query.message.reply_text(label)

    elif data.startswith("course_"):
        ctype = data.split("_")[1]
        if ctype == "forever":
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            user_states[chat_id]["data"]["course_type"] = ctype
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "start_course":
        meds = [n for n, m in data_store.get(chat_id, {}).items() if not m.get("is_started")]
        if not meds: 
            await query.message.reply_text("Нет курсов для запуска.")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("start_now:"):
        name = data.split(":")[1]
        data_store[chat_id][name].update({"is_started": True, "start_date": get_now()})
        await query.message.reply_text(f"▶️ Курс «{name}» начат!", reply_markup=main_menu())

    elif data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("open_days:"):
        name = data.split(":")[1]
        await query.message.reply_text(f"📅 Настройка: {name}", reply_markup=days_menu(name, data_store[chat_id][name]["times"]))

    elif data.startswith("set_day:"):
        _, name, day = data.split(":")
        user_states[chat_id] = {"flow": "set_reminder", "medicine": name, "day_key": day}
        await query.message.reply_text(f"Введите время для {DAYS_MAP[day]} (напр. 8:00, 20:00):")

    elif data.startswith("taken:"):
        name = data.split(":")[1]
        snoozed_alerts.pop(f"{chat_id}_{name}", None)
        await query.edit_message_text(f"✅ {name}: принято!")

    elif data.startswith("snooze:"):
        name = data.split(":")[1]
        snoozed_alerts[f"{chat_id}_{name}"] = (get_now() + timedelta(minutes=20)).strftime("%H:%M")
        await query.edit_message_text(f"⏳ {name}: напомню через 20 минут")

    elif data == "summary": await show_summary(query)
    elif data == "forecast": await show_forecast(query)
    elif data in ["dose", "refill", "delete"]:
        meds = list(data_store.get(chat_id, {}).keys())
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        action, med = data.split(":", 1)
        if action == "delete":
            data_store[chat_id].pop(med, None)
            await query.message.reply_text("🗑 Удалено", reply_markup=main_menu())
        elif action == "dose":
            user_states[chat_id] = {"flow": "dose", "medicine": med}
            await query.message.reply_text("Введите новую дозу:")
        elif action == "refill":
            user_states[chat_id] = {"flow": "refill", "medicine": med, "step": "unit_mg", "data": {}}
            await query.message.repl
