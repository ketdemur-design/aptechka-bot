import os
import asyncio
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import pytz
from settings import APP_VERSION
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== КОНФИГУРАЦИЯ ==================
# Версия приложения (меняйте в settings.py или через APP_VERSION в окружении)
TZ_MOSCOW = pytz.timezone('Europe/Moscow')
BOT_VERSION = APP_VERSION
DATA_FILE = Path("meds_data.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

data_store = {}
user_states = {}
started_users = set()
pending_delayed_tasks = set()
_write_lock = asyncio.Lock()
bot_application = None


# Для локальной отладки
DATA_FILE = Path("meds_data.json")

# Для локальной отладки

print(f"🤖 Бот использует файл данных: {DATA_FILE.absolute()}")

# ================== PYDANTIC МОДЕЛИ ДЛЯ API ==================
class AddMedicineRequest(BaseModel):
    name: str
    form: str
    unit_mg: float
    units: float
    daily_mg: float
    course_days: int = 0

class UpdateMedicineRequest(BaseModel):
    chat_id: int
    med_name: str
    daily_mg: Optional[float] = None
    add_stock: Optional[float] = None

class StartCourseRequest(BaseModel):
    chat_id: int
    med_name: str

class TakenRequest(BaseModel):
    chat_id: int
    med_name: str

class DeleteRequest(BaseModel):
    chat_id: int
    med_name: str

# ================== СЕРИАЛИЗАЦИЯ ==================
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
            except (ValueError, TypeError):
                payload[dt_key] = None
    payload.setdefault("times", {})
    payload.setdefault("notified", False)
    payload.setdefault("is_started", False)
    payload.setdefault("last_reminder_key", None)
    payload.setdefault("last_9am_key", None)
    return payload

async def save_data_store_async():
    serializable = {
        str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
        for chat_id, meds in data_store.items()
    }
    async with _write_lock:
        temp_file = DATA_FILE.with_suffix(".tmp")
        temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(DATA_FILE)

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
        raw_text = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw_text:
            data_store = {}
            return
        raw = json.loads(raw_text)
        data_store.clear()
        for chat_id, meds in raw.items():
            data_store[int(chat_id)] = {name: _deserialize_med(med) for name, med in meds.items()}
        total = sum(len(v) for v in data_store.values())
        print(f"Загружено записей о лекарствах: {total}")
    except Exception as e:
        print(f"Ошибка при загрузке данных: {e}")
        data_store = {}

# ================== ХЕЛПЕРЫ ==================
def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    if not med.get("daily_mg") or med["daily_mg"] <= 0:
        return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    start_dt = med["start_date"]
    if isinstance(start_dt, str):
        try:
            start_dt = datetime.fromisoformat(start_dt)
            if start_dt.tzinfo is None:
                start_dt = TZ_MOSCOW.localize(start_dt)
        except Exception:
            return capacity_days
    days_passed = (get_now() - start_dt).days
    return max(0, capacity_days - days_passed)

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    if form == "spray":
        return "мл", "впрыскиваний"
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"

def format_schedule(times_dict):
    if not isinstance(times_dict, dict):
        return "не установлено"
    if times_dict.get("Everyday"):
        return f"Каждый день: {', '.join(times_dict['Everyday'])}"
    lines = []
    for day in sorted(k for k in times_dict if times_dict[k]):
        day_name = {"0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт", "4": "Пт", "5": "Сб", "6": "Вс"}.get(day, day)
        lines.append(f"{day_name}: {', '.join(times_dict[day])}")
    return "\n".join(lines) if lines else "не указано"

def calculate_progress(med):
    """Рассчет прогресса курса в процентах"""
    if not med.get("course_days") or med["course_days"] <= 0:
        return 0
    if not med.get("is_started") or not med.get("start_date"):
        return 0
    start_dt = med["start_date"]
    if isinstance(start_dt, str):
        try:
            start_dt = datetime.fromisoformat(start_dt)
            if start_dt.tzinfo is None:
                start_dt = TZ_MOSCOW.localize(start_dt)
        except Exception:
            return 0
    days_passed = (get_now() - start_dt).days
    progress = min(100, int((days_passed / med["course_days"]) * 100))
    return max(0, progress)

# ================== МЕНЮ ДЛЯ БОТА ==================
DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

FORM_LABELS = {
    "tablets":  ("таблетке",  "таблеток"),
    "capsules": ("капсуле",   "капсул"),
    "liquid":   ("бутылке",   "бутылок"),
    "drops":    ("флаконе",   "флаконов"),
    "spray":    ("флаконе",   "флаконов"),
    "sachet":   ("саше",      "саше"),
}

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
        [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets"),
         InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops"),
         InlineKeyboardButton("💨 Спрей", callback_data="form_spray")],
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet"),
         InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
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
    for key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"📅 {DAYS_MAP[key]}"
        if times_dict.get(key):
            text += f" ({', '.join(times_dict[key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{key}")])
    for i in range(7):
        day_key = str(i)
        text = DAYS_MAP[day_key]
        if times_dict.get(day_key):
            text += f" ({', '.join(times_dict[day_key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{day_key}")])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def reminder_action_menu(med_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 10м", callback_data=f"later:10:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 20м", callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 30м", callback_data=f"later:30:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 1 час", callback_data=f"later:60:{med_name}")],
    ])

# ================== ТЕЛЕГРАМ БОТ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"Привет 👋 (v{BOT_VERSION})\n\n"
        "Я работаю по московскому времени (MSK).\n"
        "Я помогу:\n"
        "• следить за остатками лекарств 💊\n"
        "• напоминать о приеме по времени ⏰\n"
        "• напоминать о покупке за 7 дней\n\n"
        "Меню управления — внизу экрана 👇",
        reply_markup=main_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in {"старт", "начать", "start", "/start", "🚀 старт", "меню старт"}:
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await update.message.reply_text(f"Бот перезапущен (v{BOT_VERSION}). Главное меню:", reply_markup=main_menu())
        return

    if text == "➕ Добавить лекарство":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await update.message.reply_text("Введите название лекарства:")
        return
    elif text == "▶️ Начать курс":
        meds = data_store.get(chat_id, {})
        not_started = [name for name, m in meds.items() if not m.get("is_started")]
        if not not_started:
            await update.message.reply_text("Нет лекарств для запуска или все курсы уже начаты.")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in not_started]
        await update.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text in ("🔄 Докуплено / Пополнить", "♻️ Докуплено / Пополнить"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"refill:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text in ("🔧 Изменить дозировку", "🛠️ Изменить дозировку"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"dose:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text == "⏰ Напоминание (Дни/Время)":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство для настройки расписания:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text == "📋 Мои курсы и прогноз":
        await show_summary(update, context)
        return
    elif text == "🗑 Удалить лекарство":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"delete:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return

    state = user_states.get(chat_id)
    if not state:
        return

    d = state.get("data", {})

    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())
        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text.replace(",", "."))
            _, plural = FORM_LABELS.get(d["form"], ("единице", "единиц"))
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")
        elif state["step"] == "units":
            d["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
            form = d.get("form")
            if form == "drops":
                await update.message.reply_text("Сколько капель в сутки назначено?")
            elif form == "spray":
                await update.message.reply_text("Сколько впрыскиваний в сутки назначено?")
            elif form == "liquid":
                await update.message.reply_text("Сколько мл в сутки назначено?")
            else:
                await update.message.reply_text("Сколько мг в сутки принимаете?")
        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())
        elif state["step"] == "course_value":
            value = int(float(text.replace(",", ".")))
            if d.get("course_type") == "months":
                value *= 30
            d["course_days"] = value
            await save_medicine(update, chat_id)
        return

    elif state["flow"] == "set_reminder":
        try:
            med_name = state["medicine"]
            day_key = state["day_key"]
            med_data = data_store[chat_id][med_name]
            if text.lower() in ["0", "нет", "удалить"]:
                med_data["times"].pop(day_key, None)
                status_msg = f"🗑 Удалено время для {DAYS_MAP.get(day_key, day_key)}"
            else:
                times = parse_times(text)
                if not times:
                    await update.message.reply_text("⚠️ Неверный формат. Пример: 08:00, 20:00")
                    return
                med_data["times"][day_key] = times
                status_msg = f"✅ Сохранено для {DAYS_MAP.get(day_key, day_key)}"
            await save_data_store_async()
            user_states.pop(chat_id, None)
            await update.message.reply_text(f"{status_msg}\n\nНастройте следующий день или вернитесь в меню:", reply_markup=days_menu(med_name, med_data["times"]))
        except Exception as e:
            await update.message.reply_text("✅ Изменения применены.")
            user_states.pop(chat_id, None)
        return

    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text.replace(",", "."))
        await save_data_store_async()
        days = calc_days_left(med)
        user_states.pop(chat_id, None)
        await update.message.reply_text(f"🔧 Дозировка изменена! Теперь хватит на {days} дн.", reply_markup=main_menu())
        return

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            await update.message.reply_text("Сколько штук/флаконов купили?")
            return
        elif state["step"] == "units":
            units = int(text)
            med = data_store[chat_id][state["medicine"]]
            form = med.get("form", "tablets")
            unit_size = state["data"]["unit_mg"]

            if form == "drops":
                added = (unit_size / 0.05) * units
            elif form == "spray":
                added = (unit_size / 0.1) * units
            else:
                added = unit_size * units

            med["total_mg"] += added
            med["notified"] = False
            await save_data_store_async()
            days = calc_days_left(med)
            user_states.pop(chat_id, None)
            await update.message.reply_text(f"🔄 Пополнено! Хватит на {days} дн.", reply_markup=main_menu())
            return

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "main_menu":
        user_states.pop(chat_id, None)
        await query.message.reply_text("Возврат в главное меню:", reply_markup=main_menu())
        return
    if data.startswith("form_"):
        form = data.split("_")[1]
        user_states[chat_id]["data"]["form"] = form
        user_states[chat_id]["step"] = "unit_mg"
        if form == "drops":
            await query.message.reply_text("Объем флакона (мл)?")
        elif form in ("spray", "liquid"):
            await query.message.reply_text("Сколько мл в одной единице?")
        else:
            singular, _ = FORM_LABELS.get(form, ("единице", "единиц"))
            await query.message.reply_text(f"Сколько мг в одной {singular}?")
        return
    if data.startswith("course_"):
        state = user_states[chat_id]
        d = state["data"]
        ctype = data.split("_")[1]
        d["course_type"] = ctype
        if ctype == "forever":
            d["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")
        return
    if data.startswith("start_now:"):
        _, med_name = data.split(":", 1)
        med = data_store[chat_id][med_name]
        med["is_started"] = True
        med["start_date"] = get_now()
        save_data_store()
        await query.message.reply_text(f"▶️ Курс «{med_name}» начат!\nОбратный отсчет запущен.", reply_markup=main_menu())
        return
    if data.startswith("open_days:"):
        _, med_name = data.split(":", 1)
        if med_name in data_store.get(chat_id, {}):
            med_data = data_store[chat_id][med_name]
            if not isinstance(med_data.get("times"), dict):
                med_data["times"] = {}
            await query.message.reply_text(f"📅 Настройка: «{med_name}».\nВыберите день недели:", reply_markup=days_menu(med_name, med_data["times"]))
        return
    if data.startswith("set_day:"):
        _, med_name, day_key = data.split(":", 2)
        user_states[chat_id] = {"flow": "set_reminder", "medicine": med_name, "day_key": day_key}
        day_label = DAYS_MAP.get(day_key, day_key)
        await query.edit_message_text(f"⏰ Введите время для «{med_name}» ({day_label}).\nФормат: 8:00, 20:00 (через запятую).\nОтправьте «0» или «удалить», чтобы очистить день.", reply_markup=None)
        return
    if data.startswith("taken:"):
        _, med_name = data.split(":", 1)
        # Обновляем запас
        med = data_store[chat_id][med_name]
        times_dict = med.get("times", {})
        doses_count = 1
        for times in times_dict.values():
            if times:
                doses_count = max(doses_count, len(times))
        per_dose = med["daily_mg"] / doses_count if doses_count > 0 else med["daily_mg"]
        med["total_mg"] -= per_dose
        save_data_store()
        await query.edit_message_text(f"✅ Прием «{med_name}» отмечен. Молодец!")
        return
    if data.startswith("later:"):
        parts = data.split(":", 2)
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        med_name = parts[2] if len(parts) > 2 else ""
        if context.job_queue:
            context.job_queue.run_once(send_delayed_reminder, when=minutes * 60, data={"chat_id": chat_id, "med_name": med_name})
        await query.edit_message_text(f"⏳ Хорошо, напомню про «{med_name}» через {minutes} мин.")
        return
    if ":" in data:
        action, med = data.split(":", 1)
        if action in ("dose", "refill", "delete"):
            if action == "dose":
                user_states[chat_id] = {"flow": "dose", "medicine": med}
                await query.message.reply_text("Введите новую суточную дозировку:")
            elif action == "refill":
                user_states[chat_id] = {"flow": "refill", "medicine": med, "step": "unit_mg", "data": {}}
                await query.message.reply_text("Сколько мг в одной единице?")
            elif action == "delete":
                data_store[chat_id].pop(med, None)
                save_data_store()
                await query.edit_message_text("🗑 Лекарство удалено")

async def save_medicine(update_or_query, chat_id):
    d = user_states[chat_id]["data"]
    form = d["form"]
    unit_mg = d["unit_mg"]
    units = d["units"]
    daily_mg = d["daily_mg"]
    course_days = d.get("course_days")

    if form == "drops":
        total_resource = (unit_mg / 0.05) * units
    elif form == "spray":
        total_resource = (unit_mg / 0.1) * units
    else:
        total_resource = unit_mg * units

    data_store.setdefault(chat_id, {})[d["name"]] = {
        "form": form,
        "daily_mg": daily_mg,
        "unit_mg": unit_mg,
        "total_mg": total_resource,
        "course_days": course_days,
        "created": get_now(),
        "is_started": False,
        "start_date": None,
        "times": {},
        "notified": False,
        "last_reminder_key": None,
        "last_9am_key": None,
    }

    await save_data_store_async()
    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    _, dose_label = get_display_units(med)

    msg = f"✅ Лекарство *{d['name']}* успешно добавлено!\n\nРасход: {daily_mg} {dose_label}/сутки\nХватит на: {days} дней\n\n⚠️ Нажмите «▶️ Начать курс» для старта отсчёта."

    if hasattr(update_or_query, 'reply_text'):
        await update_or_query.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
    else:
        await update_or_query.edit_message_text(msg, parse_mode="Markdown")
        await update_or_query.message.reply_text("Главное меню:", reply_markup=main_menu())

    user_states.pop(chat_id, None)

async def show_summary(update_or_query, context: ContextTypes.DEFAULT_TYPE = None):
    if hasattr(update_or_query, "message"):
        message = update_or_query.message
        chat_id = message.chat.id
    else:
        message = update_or_query.callback_query.message
        chat_id = message.chat.id

    meds = data_store.get(chat_id, {})
    if not meds:
        await message.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка и прогноз:\n\n"
    for name, med in meds.items():
        days_left = calc_days_left(med)
        status = "▶️ Идет прием" if med.get("is_started") else "⏸ Ожидание старта"
        schedule = format_schedule(med.get("times", {}))
        _, dose_label = get_display_units(med)

        msg += f"💊 {name} ({status})\n"
        msg += f"Расписание: {schedule}\n"
        msg += f"Хватит на: {days_left} дн. при расходе {med['daily_mg']} {dose_label}/сут\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"

    await message.reply_text(msg.strip(), reply_markup=main_menu())

async def reminder_loop():
    while True:
        try:
            now_msk = get_now()
            current_time_str = now_msk.strftime("%H:%M")
            current_weekday = str(now_msk.weekday())
            is_weekend = now_msk.weekday() >= 5

            for chat_id, meds in list(data_store.items()):
                for name, m in list(meds.items()):
                    if not m.get("is_started"):
                        continue
                    t_dict = m.get("times", {})
                    times_to_check = []
                    if t_dict.get("Everyday"):
                        times_to_check.extend(t_dict["Everyday"])
                    if is_weekend and t_dict.get("Weekends"):
                        times_to_check.extend(t_dict["Weekends"])
                    if not is_weekend and t_dict.get("Weekdays"):
                        times_to_check.extend(t_dict["Weekdays"])
                    if t_dict.get(current_weekday):
                        times_to_check.extend(t_dict[current_weekday])
                    
                    if current_time_str in set(times_to_check):
                        reminder_key = f"{now_msk.date().isoformat()}:{current_time_str}:{name}"
                        if m.get("last_reminder_key") == reminder_key:
                            continue
                        _, dose_label = get_display_units(m)
                        doses_count = len(set(times_to_check)) or 1
                        per_dose = m["daily_mg"] / doses_count
                        if bot_application:
                            await bot_application.bot.send_message(
                                chat_id,
                                f"⏰ Время принимать лекарство: {name}\nДозировка: {per_dose:.1f} {dose_label}",
                                reply_markup=reminder_action_menu(name)
                            )
                        m["last_reminder_key"] = reminder_key
                        save_data_store()
        except Exception as e:
            print(f"Ошибка в цикле напоминаний: {e}")
        await asyncio.sleep(30)

async def send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    med_name = job.data["med_name"]
    chat_id = job.data["chat_id"]
    meds = data_store.get(chat_id, {})
    if med_name in meds and bot_application:
        m = meds[med_name]
        _, dose_label = get_display_units(m)
        await bot_application.bot.send_message(chat_id, f"🔔 Напоминание: {med_name}\nДозировка: {m['daily_mg']} {dose_label}", reply_markup=reminder_action_menu(med_name))

def parse_times(text):
    text = text.replace("24:00", "00:00")
    clean_text = (text.replace(",", " ").replace(";", " ")
                  .replace(".", ":").replace("\n", " "))
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

# ================== ЗАПУСК ==================
async def post_init(application):
    global bot_application
    bot_application = application
    await application.bot.set_my_commands([BotCommand("start", "перезапустить бота")])
    load_data_store()
    asyncio.create_task(reminder_loop())
    print(f"Бот v{BOT_VERSION} инициализирован. DATA_FILE={DATA_FILE}")

def main():
    app_bot = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(buttons))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print(f"Telegram бот v{BOT_VERSION} запускается...")
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
