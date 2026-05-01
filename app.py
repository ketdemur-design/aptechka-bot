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
)

# --- CONFIG ---
TZ_MOSCOW = pytz.timezone('Europe/Moscow')
BOT_VERSION = "1.1.26"
# Важно: путь /data/ нужен для работы в Docker на хостинге
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

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "liquid": ("бутылке", "бутылок"),
    "drops": ("флаконе", "флаконов"),
    "spray": ("флаконе", "флаконов"),
    "sachet": ("саше", "саше"),
}

# --- DATA STORAGE ---
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
    serializable = {str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
                    for chat_id, meds in data_store.items()}
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(DATA_FILE)

def load_data_store():
    global data_store
    if not DATA_FILE.exists(): return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data_store.clear()
        for chat_id, meds in raw.items():
            data_store[int(chat_id)] = {name: _deserialize_med(med) for name, med in meds.items()}
    except: pass

# --- UI ---
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

def days_menu(med_name, times_dict=None):
    if not isinstance(times_dict, dict): times_dict = {}
    keyboard = []
    for key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"📅 {DAYS_MAP[key]}"
        if times_dict.get(key): text += f" ({', '.join(times_dict[key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{key}")])
    for i in range(7):
        day_key = str(i)
        text = DAYS_MAP[day_key]
        if times_dict.get(day_key): text += f" ({', '.join(times_dict[day_key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{day_key}")])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def reminder_action_menu(med_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"taken_alert:{med_name}")],
        [InlineKeyboardButton("⏰ Через 20м", callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Через 1 час", callback_data=f"later:60:{med_name}")]
    ])

# --- LOGIC ---
def get_now(): return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    if not med.get("daily_mg") or med["daily_mg"] <= 0: return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"): return capacity_days
    days_passed = (get_now() - med["start_date"]).days
    return max(0, capacity_days - days_passed)

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops": return "мл", "капель"
    if form == "spray": return "мл", "впрыскиваний"
    if form == "liquid": return "мл", "мл"
    return "мг", "мг"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(f"Бот аптечка запущен (v{BOT_VERSION})", reply_markup=main_menu())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Кнопки меню
    if text == "➕ Добавить лекарство":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await update.message.reply_text("Введите название лекарства:")
        return
    elif text == "▶️ Начать курс":
        meds = data_store.get(chat_id, {})
        not_started = [name for name, m in meds.items() if not m.get("is_started")]
        if not not_started:
            await update.message.reply_text("Нет лекарств для запуска.")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in not_started]
        await update.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text == "📋 Мои курсы и прогноз":
        meds = data_store.get(chat_id, {})
        if not meds:
            await update.message.reply_text("Список пуст.")
            return
        res = "📋 Ваши лекарства:\n\n"
        for name, med in meds.items():
            days = calc_days_left(med)
            res += f"💊 {name}: осталось на {days} дн.\n"
        await update.message.reply_text(res)
        return
    elif text == "🗑 Удалить лекарство":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"delete:{m}")] for m in meds]
        await update.message.reply_text("Что удалить?", reply_markup=InlineKeyboardMarkup(kb))
        return

    # Логика шагов добавления
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
            await update.message.reply_text("Сколько штук/флаконов купили?")
        elif state["step"] == "units":
            d["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько в сутки назначено?")
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
        await query.message.reply_text("Дозировка/Объем одной единицы?")
    elif data.startswith("start_now:"):
        name = data.split(":")[1]
        data_store[chat_id][name].update({"is_started": True, "start_date": get_now()})
        save_data_store()
        await query.edit_message_text(f"▶️ Курс {name} запущен!")
    elif data.startswith("delete:"):
        name = data.split(":")[1]
        data_store[chat_id].pop(name, None)
        save_data_store()
        await query.edit_message_text(f"🗑 Удалено: {name}")

async def save_medicine(update_or_query, chat_id):
    d = user_states[chat_id]["data"]
    total = d["unit_mg"] * d["units"]
    if d["form"] == "drops": total = (d["unit_mg"] / 0.05) * d["units"]
    elif d["form"] == "spray": total = (d["unit_mg"] / 0.1) * d["units"]
    
    data_store.setdefault(chat_id, {})[d["name"]] = {
        "form": d["form"], "daily_mg": d["daily_mg"], "unit_mg": d["unit_mg"],
        "total_mg": total, "course_days": d.get("course_days"),
        "created": get_now(), "is_started": False, "start_date": None, "times": {}
    }
    save_data_store()
    msg = f"✅ {d['name']} добавлено!"
    if hasattr(update_or_query, 'message'): 
        await update_or_query.message.reply_text(msg, reply_markup=main_menu())
    else: 
        await update_or_query.edit_message_text(msg)
    user_states.pop(chat_id)

# --- SYSTEM ---
async def reminder_loop(application):
    while True:
        try:
            now = get_now()
            t_str = now.strftime("%H:%M")
            for chat_id, meds in data_store.items():
                for name, m in meds.items():
                    if m.get("is_started"):
                        # Тут можно добавить логику проверки времени
                        pass
        except: pass
        await asyncio.sleep(60)

async def post_init(application):
    await application.bot.set_my_commands([BotCommand("start", "перезапустить бота")])
    asyncio.create_task(reminder_loop(application))

def main():
    load_data_store()
    token = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
