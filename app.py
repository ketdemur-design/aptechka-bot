import os
import asyncio
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
import pytz
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
TZ_MOSCOW = pytz.timezone('Europe/Moscow')
BOT_VERSION = "1.1.27"
DATA_FILE = Path(os.getenv("DATA_FILE", "/data/meds_data.json"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

data_store = {}
user_states = {}
started_users = set()
pending_delayed_tasks = set()

# Asyncio lock для безопасной записи (бот и веб-сервер не повредят файл)
_write_lock = asyncio.Lock()

# ================== СЕРИАЛИЗАЦИЯ / ДЕСЕРИАЛИЗАЦИЯ ==================

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
    return payload


async def save_data_store_async():
    """Асинхронная запись с asyncio.Lock — безопасно при совместном доступе бота и веб-сервера."""
    serializable = {
        str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
        for chat_id, meds in data_store.items()
    }
    async with _write_lock:
        temp_file = DATA_FILE.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_file.replace(DATA_FILE)


def save_data_store():
    """Синхронная обёртка — используется в местах без await (для совместимости)."""
    serializable = {
        str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
        for chat_id, meds in data_store.items()
    }
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_file.replace(DATA_FILE)


def load_data_store():
    global data_store
    if not DATA_FILE.exists():
        print(f"Файл {DATA_FILE} не найден, начинаем с пустого списка.")
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
            data_store[int(chat_id)] = {
                name: _deserialize_med(med) for name, med in meds.items()
            }
        total = sum(len(v) for v in data_store.values())
        print(f"Загружено записей о лекарствах: {total}")
    except Exception as e:
        print(f"Ошибка при загрузке данных: {e}")
        data_store = {}

# ================== МЕНЮ И СЛОВАРИ ==================

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


def start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Перезапустить бота", callback_data="start_bot")]
    ])


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
        [InlineKeyboardButton("💊 Таблетки",      callback_data="form_tablets"),
         InlineKeyboardButton("💊 Капсулы",       callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops"),
         InlineKeyboardButton("💨 Спрей",          callback_data="form_spray")],
        [InlineKeyboardButton("📦 Саше",           callback_data="form_sachet"),
         InlineKeyboardButton("🧴 Жидкая форма",  callback_data="form_liquid")],
    ])


def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни",              callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно",        callback_data="course_forever")],
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
    """Меню действий при напоминании о приёме."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил",                  callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 10м",   callback_data=f"later:10:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 20м",   callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 30м",   callback_data=f"later:30:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 1 час", callback_data=f"later:60:{med_name}")],
    ])

# ================== ХЕЛПЕРЫ ==================

def get_now():
    """Текущее время по Москве."""
    return datetime.now(TZ_MOSCOW)


def calc_days_left(med):
    """Сколько дней хватит остатка с учётом уже прошедшего времени."""
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
    """Возвращает (единица_объёма, единица_дозы) для нужной формы."""
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    if form == "spray":
        return "мл", "впрыскиваний"
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"


def parse_times(text):
    """Парсинг времени, включая поддержку 24:00 → 00:00."""
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


def format_schedule(times_dict):
    """Форматирует расписание для вывода в тексте."""
    if not isinstance(times_dict, dict):
        return "не установлено"
    if times_dict.get("Everyday"):
        return f"Каждый день: {', '.join(times_dict['Everyday'])}"
    lines = []
    for day in sorted(k for k in times_dict if times_dict[k]):
        lines.append(f"{DAYS_MAP.get(day, day)}: {', '.join(times_dict[day])}")
    return "\n".join(lines) if lines else "не установлено"

# ================== /start ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"Привет 👋 (v{BOT_VERSION})\n\n"
        "Я работаю по московскому времени (MSK).\n"
        "Я помогу:\n"
        "• следить за остатками лекарств 💊\n"
        "• напоминать о приеме по времени (по дням недели) ⏰\n"
        "• напоминать о покупке за 7 дней\n\n"
        "Меню управления — внизу экрана 👇",
        reply_markup=main_menu()
    )

# ================== TEXT HANDLER ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Перезапуск бота
    if text.lower() in {
        "старт", "начать", "start", "/start",
        "🚀 старт", "меню старт", "старт меню",
        "🚀 перезапустить бота",
    }:
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await update.message.reply_text(
            f"Бот перезапущен (v{BOT_VERSION}). Главное меню:",
            reply_markup=main_menu(),
        )
        return

    # ── Нижние кнопки ──────────────────────────────────────────────────────────
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
        await update.message.reply_text("Выберите лекарство для настройки расписания:",
                                        reply_markup=InlineKeyboardMarkup(kb))
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
        await update.message.reply_text("Выберите лекарство для удаления:",
                                        reply_markup=InlineKeyboardMarkup(kb))
        return

    # ── Логика ввода (flow) ─────────────────────────────────────────────────────
    state = user_states.get(chat_id)
    if not state:
        if chat_id not in started_users:
            await start(update, context)
        return

    d = state.get("data", {})

    # ── ADD ───────────────────────────────────────────────────────────────────
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

    # ── SET REMINDER ──────────────────────────────────────────────────────────
    elif state["flow"] == "set_reminder":
        try:
            med_name = state["medicine"]
            day_key  = state["day_key"]

            if chat_id not in data_store or med_name not in data_store[chat_id]:
                await update.message.reply_text("❌ Ошибка: лекарство не найдено.")
                user_states.pop(chat_id, None)
                return

            med_data = data_store[chat_id][med_name]
            if "times" not in med_data or not isinstance(med_data["times"], dict):
                med_data["times"] = {}

            if text.lower() in ["0", "нет", "удалить", "off", "выкл"]:
                med_data["times"].pop(day_key, None)
                status_msg = f"🗑 Удалено время для «{med_name}» ({DAYS_MAP.get(day_key, day_key)})"
            else:
                times = parse_times(text)
                if not times:
                    await update.message.reply_text(
                        "⚠️ Не удалось распознать время. Введите в формате ЧЧ:ММ (или '0' для удаления)"
                    )
                    return

                if day_key == "Everyday":
                    med_data["times"] = {"Everyday": times}
                else:
                    med_data["times"].pop("Everyday", None)
                    med_data["times"][day_key] = times

                status_msg = f"✅ Сохранено: {DAYS_MAP.get(day_key, day_key)} — {', '.join(times)}"

            save_data_store()
            user_states.pop(chat_id)
            await update.message.reply_text(
                f"{status_msg}\n\nВыберите следующий день для настройки:",
                reply_markup=days_menu(med_name, med_data["times"])
            )

        except Exception as e:
            print(f"Ошибка в set_reminder: {e}")
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте снова.")
            user_states.pop(chat_id, None)

    # ── CHANGE DOSE ───────────────────────────────────────────────────────────
    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text.replace(",", "."))
        med["notified"] = False
        save_data_store()

        days = calc_days_left(med)
        unit_label, dose_label = get_display_units(med)
        dosage = med["unit_mg"]

        msg = (
            f"🔧 Дозировка изменена\n\n"
            f"Дозировка: {dosage:g} {unit_label}\n"
            f"Теперь хватает на: {days} дней при расходе {med['daily_mg']:g} {dose_label}/сутки."
        )
        await update.message.reply_text(msg, reply_markup=main_menu())
        user_states.pop(chat_id)

    # ── REFILL ────────────────────────────────────────────────────────────────
    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            _, plural = FORM_LABELS.get(state.get("form", "tablets"), ("единице", "единиц"))
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text.replace(",", ".")))
            med_name = state["medicine"]
            med = data_store[chat_id][med_name]
            new_unit_size = state["data"]["unit_mg"]

            if med.get("form") == "drops":
                added_resource = (new_unit_size / 0.05) * units
            elif med.get("form") == "spray":
                added_resource = (new_unit_size / 0.1) * units
            else:
                added_resource = new_unit_size * units

            med["total_mg"] += added_resource
            med["notified"] = False
            save_data_store()

            days = calc_days_left(med)
            unit_label, dose_label = get_display_units(med)

            msg = (
                f"🔄 Лекарство пополнено\n\n"
                f"Название: {med_name}\n"
                f"Добавлено: {units} ед. по {new_unit_size:g} {unit_label}\n"
                f"Хватит на: {days} дней"
            )

            if med.get("course_days"):
                msg += f"\nДлительность курса: {med['course_days']} дней"
                needed_resource = med["course_days"] * med["daily_mg"]
                if med["total_mg"] < needed_resource:
                    deficit = needed_resource - med["total_mg"]
                    if med.get("form") == "drops":
                        deficit_ml = deficit * 0.05
                        missing_units = deficit_ml / new_unit_size
                    elif med.get("form") == "spray":
                        deficit_ml = deficit * 0.1
                        missing_units = deficit_ml / new_unit_size
                    else:
                        missing_units = deficit / new_unit_size
                    msg += (
                        f"\n⚠️ На курс не хватит, нужно докупить "
                        f"{missing_units:g} ед. по {new_unit_size:g} {unit_label}."
                    )
                else:
                    surplus = med["total_mg"] - needed_resource
                    if surplus > 0:
                        if med.get("form") in ("drops", "spray"):
                            factor = 0.05 if med["form"] == "drops" else 0.1
                            surplus_units = (surplus * factor) / new_unit_size
                        else:
                            surplus_units = surplus / new_unit_size
                        msg += f"\n✅ На курс хватит, останется излишек {surplus_units:g} ед."
                    else:
                        msg += "\n✅ На курс хватит ровно."

            await update.message.reply_text(msg, reply_markup=main_menu())
            user_states.pop(chat_id)

# ================== КНОПКИ (CALLBACKS) ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data    = query.data

    if data == "start_bot":
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await query.message.reply_text(
            f"Бот перезапущен (v{BOT_VERSION}). Главное меню:",
            reply_markup=main_menu()
        )
        return

    if data == "main_menu":
        user_states.pop(chat_id, None)
        await query.message.reply_text("Возврат в главное меню:", reply_markup=main_menu())
        return

    if data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")
        return

    if data.startswith("taken:"):
        _, med_name = data.split(":", 1)
        await query.edit_message_text(f"✅ Прием «{med_name}» отмечен. Молодец!")
        return

    if data.startswith("later:"):
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            minutes  = int(parts[1])
            med_name = parts[2]
        else:
            minutes  = 20
            med_name = parts[1] if len(parts) > 1 else ""

        remind_at = get_now() + timedelta(minutes=minutes)
        time_str  = remind_at.strftime("%H:%M")

        if context.job_queue:
            context.job_queue.run_once(
                send_delayed_reminder,
                when=minutes * 60,
                data={"chat_id": chat_id, "med_name": med_name}
            )
        else:
            task = asyncio.create_task(
                send_delayed_reminder_fallback(context.bot, chat_id, med_name, minutes)
            )
            pending_delayed_tasks.add(task)
            task.add_done_callback(pending_delayed_tasks.discard)

        await query.edit_message_text(
            f"⏳ Хорошо, напомню про «{med_name}» через {minutes} мин. в {time_str}."
        )
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
        state  = user_states[chat_id]
        d      = state["data"]
        ctype  = data.split("_")[1]
        d["course_type"] = ctype
        if ctype == "forever":
            d["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")
        return

    if data == "start_course":
        meds = data_store.get(chat_id, {})
        not_started = [name for name, m in meds.items() if not m.get("is_started")]
        if not not_started:
            await query.message.reply_text("Нет лекарств для запуска.", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in not_started]
        await query.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("start_now:"):
        _, med_name = data.split(":", 1)
        med = data_store[chat_id][med_name]
        med["is_started"]  = True
        med["start_date"]  = get_now()
        save_data_store()
        await query.message.reply_text(
            f"▶️ Курс «{med_name}» начат!\nОбратный отсчет запущен.",
            reply_markup=main_menu()
        )
        return

    if data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("open_days:"):
        _, med_name = data.split(":", 1)
        if med_name in data_store.get(chat_id, {}):
            med_data = data_store[chat_id][med_name]
            if not isinstance(med_data.get("times"), dict):
                med_data["times"] = {}
            await query.message.reply_text(
                f"📅 Настройка: «{med_name}».\nВыберите день недели:",
                reply_markup=days_menu(med_name, med_data["times"])
            )
        return

    if data.startswith("set_day:"):
        _, med_name, day_key = data.split(":", 2)
        user_states[chat_id] = {
            "flow":     "set_reminder",
            "medicine": med_name,
            "day_key":  day_key
        }
        day_label = DAYS_MAP.get(day_key, day_key)
        await query.edit_message_text(
            f"⏰ Введите время для «{med_name}» ({day_label}).\n"
            f"Формат: 8:00, 20:00 (через запятую).\n"
            f"Отправьте «0» или «удалить», чтобы очистить день.",
            reply_markup=None
        )
        return

    if data in ("dose", "refill", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if ":" in data:
        action, med = data.split(":", 1)

        if action == "dose":
            med_data = data_store[chat_id][med]
            user_states[chat_id] = {"flow": "dose", "medicine": med, "data": {}}
            if med_data.get("form") == "drops":
                await query.message.reply_text("Введите новую суточную дозировку (капель):")
            elif med_data.get("form") in ("spray", "liquid"):
                await query.message.reply_text("Введите новую суточную дозировку (мл):")
            else:
                await query.message.reply_text("Введите новую суточную дозировку (мг):")

        elif action == "refill":
            med_data = data_store[chat_id][med]
            form = med_data.get("form", "tablets")
            user_states[chat_id] = {
                "flow":     "refill",
                "medicine": med,
                "step":     "unit_mg",
                "form":     form,
                "data":     {}
            }
            if form == "drops":
                await query.message.reply_text("Объем флакона (мл)?")
            elif form in ("spray", "liquid"):
                await query.message.reply_text("Сколько мл в одной единице?")
            else:
                await query.message.reply_text("Сколько мг в одной единице?")

        elif action == "delete":
            data_store[chat_id].pop(med, None)
            save_data_store()
            await query.edit_message_text("🗑 Лекарство удалено")

# ================== СОХРАНЕНИЕ ЛЕКАРСТВА ==================

async def save_medicine(update_or_query, chat_id):
    d    = user_states[chat_id]["data"]
    form = d.get("form")
    unit_size   = d["unit_mg"]
    units_count = d["units"]

    if form == "drops":
        total_resource = (unit_size / 0.05) * units_count
    elif form == "spray":
        total_resource = (unit_size / 0.1) * units_count
    else:
        total_resource = unit_size * units_count

    data_store.setdefault(chat_id, {})[d["name"]] = {
        "form":        form,
        "daily_mg":    d["daily_mg"],
        "unit_mg":     d["unit_mg"],
        "total_mg":    total_resource,
        "course_days": d.get("course_days"),
        "created":     get_now(),
        "is_started":  False,
        "start_date":  None,
        "times":       {},
        "notified":    False,
        "last_reminder_key": None,
    }

    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    unit_label, dose_label = get_display_units(med)

    msg = f"✅ Лекарство добавлено\n\nНазвание: {d['name']}\n"
    if form in ("drops", "spray", "liquid"):
        msg += f"Объем флакона/ед: {d['unit_mg']:g} мл\n"
    else:
        msg += f"Дозировка ед: {d['unit_mg']:g} мг\n"
    msg += (
        f"Расход: {d['daily_mg']:g} {dose_label}/сутки\n"
        f"Хватит на: {days} дней"
        "\n\n⚠️ Нажмите «▶️ Начать курс» для старта отсчёта."
    )

    save_data_store()
    user_states.pop(chat_id)

    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(msg, reply_markup=main_menu())
    else:
        await update_or_query.edit_message_text(msg)

# ================== СВОДКА / ПРОГНОЗ ==================

async def show_summary(update_or_query, context: ContextTypes.DEFAULT_TYPE = None):
    # Поддержка вызова и из Update, и из callback_query
    if hasattr(update_or_query, "message") and update_or_query.message:
        message = update_or_query.message
        chat_id = message.chat.id
    elif hasattr(update_or_query, "callback_query"):
        message = update_or_query.callback_query.message
        chat_id = message.chat.id
    else:
        message = update_or_query
        chat_id = message.chat.id

    meds = data_store.get(chat_id, {})
    if not meds:
        await message.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка и прогноз:\n\n"
    for name, med in meds.items():
        days_left = calc_days_left(med)
        status = "▶️ Идет прием" if med.get("is_started") else "⏸ Ожидание старта"

        if not isinstance(med.get("times"), dict):
            med["times"] = {}
        schedule_text = format_schedule(med.get("times", {}))
        unit_label, dose_label = get_display_units(med)

        msg += f"💊 Название: {name} ({status})\n"
        if med.get("form") in ("drops", "spray", "liquid"):
            msg += f"Дозировка ед: {med['unit_mg']:g} мл\n"
        else:
            msg += f"Дозировка ед: {med['unit_mg']:g} мг\n"

        msg += f"Время приема:\n{schedule_text}\n"

        times_dict = med.get("times", {})
        sample_key = None
        for k in ("Everyday", "Weekdays", "Weekends"):
            if times_dict.get(k):
                sample_key = k
                break
        if sample_key:
            cnt = len(times_dict[sample_key])
            per_dose = med["daily_mg"] / cnt
            msg += f"Количество приема: {cnt} раза в день по {per_dose:g} {dose_label}\n"

        msg += f"Хватит на: {days_left} дней при расходе {med['daily_mg']:g} {dose_label}/сутки\n"

        if med.get("course_days"):
            if med.get("is_started") and med.get("start_date"):
                start_dt = med["start_date"]
                if start_dt.tzinfo is None:
                    start_dt = TZ_MOSCOW.localize(start_dt)
                now = get_now()
                days_passed   = (now - start_dt).days
                remaining_days = max(0, med["course_days"] - days_passed)
                end_date = start_dt + timedelta(days=med["course_days"])
                msg += f"Курс: еще {remaining_days} дн. (до {end_date.strftime('%d.%m.%Y')})\n"
                msg += "✅ На курс хватит\n" if days_left >= remaining_days else "⚠️ Нужно докупить!\n"
            else:
                msg += f"Курс: {med['course_days']} дней (не начат)\n"
                if days_left >= med["course_days"]:
                    msg += "✅ На курс хватит\n"
                else:
                    msg += "⚠️ На весь курс не хватит\n"
        else:
            msg += "♾ Приём: бессрочно\n"

        msg += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"

    await message.reply_text(msg.strip(), reply_markup=main_menu())

# ================== ФОНОВЫЙ ЦИКЛ НАПОМИНАНИЙ ==================

async def reminder_loop(application):
    print("Фоновый цикл напоминаний запущен!")
    while True:
        try:
            now_msk          = get_now()
            current_time_str = now_msk.strftime("%H:%M")
            current_weekday  = str(now_msk.weekday())
            is_weekend       = now_msk.weekday() >= 5

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

                    times_for_today = set(times_to_check)

                    if current_time_str in times_for_today:
                        reminder_key = (
                            f"{now_msk.date().isoformat()}:{current_time_str}:{name}"
                        )
                        if m.get("last_reminder_key") == reminder_key:
                            continue

                        _, dose_label = get_display_units(m)
                        doses_count   = len(times_for_today)
                        per_dose = m["daily_mg"] / doses_count if doses_count > 0 else m["daily_mg"]

                        await application.bot.send_message(
                            chat_id,
                            f"⏰ Время принимать лекарство: {name}\n"
                            f"Дозировка: {per_dose:g} {dose_label}",
                            reply_markup=reminder_action_menu(name)
                        )
                        m["last_reminder_key"] = reminder_key
                        save_data_store()

                    # Уведомление об остатке в 9:00
                    if now_msk.hour == 9 and now_msk.minute == 0:
                        r_key_9am = f"{now_msk.date().isoformat()}:09am:{name}"
                        if m.get("last_9am_key") != r_key_9am:
                            days = calc_days_left(m)
                            if 0 < days <= 7 and not m.get("notified"):
                                await application.bot.send_message(
                                    chat_id,
                                    f"🛒 Заканчивается {name}\nХватит на: {days} дн.\nПора купить 💊"
                                )
                                m["notified"] = True
                            m["last_9am_key"] = r_key_9am
                            save_data_store()

                    # Сброс флага уведомления в полночь
                    if now_msk.hour == 0 and now_msk.minute == 0:
                        m["notified"] = False

        except Exception as e:
            print(f"Ошибка в цикле напоминаний: {e}")

        await asyncio.sleep(30)

# ================== ОТЛОЖЕННЫЕ НАПОМИНАНИЯ ==================

async def send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    job      = context.job
    med_name = job.data["med_name"]
    chat_id  = job.data["chat_id"]
    meds     = data_store.get(chat_id, {})

    if med_name in meds:
        m = meds[med_name]
        _, dose_label = get_display_units(m)
        await context.bot.send_message(
            chat_id,
            f"🔔 Напоминание: {med_name}\nДозировка: {m['daily_mg']:g} {dose_label}",
            reply_markup=reminder_action_menu(med_name)
        )


async def send_delayed_reminder_fallback(bot, chat_id: int, med_name: str, minutes: int):
    await asyncio.sleep(minutes * 60)
    meds = data_store.get(chat_id, {})
    if med_name not in meds:
        return
    m = meds[med_name]
    _, dose_label = get_display_units(m)
    await bot.send_message(
        chat_id,
        f"🔔 Напоминание: {med_name}\nДозировка: {m['daily_mg']:g} {dose_label}",
        reply_markup=reminder_action_menu(med_name)
    )

# ================== POST_INIT И MAIN ==================

async def post_init(application):
    """Вызывается PTB после инициализации Application.
    Регистрируем команды, загружаем данные и запускаем фоновый цикл."""
    await application.bot.set_my_commands([
        BotCommand("start", "перезапустить бота"),
    ])
    load_data_store()
    # asyncio.create_task — корректный способ запуска фоновой корутины.
    # event loop уже существует к моменту вызова post_init.
    asyncio.create_task(reminder_loop(application))
    print(f"Бот v{BOT_VERSION} инициализирован. DATA_FILE={DATA_FILE}")
    print("Фоновый цикл напоминаний запущен!")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print(f"Бот v{BOT_VERSION} запускается...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
