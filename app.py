import os 
import asyncio
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
import pytz
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove # Добавьте это к остальным импортам

from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram import BotCommand

TZ_MOSCOW = pytz.timezone('Europe/Moscow')
BOT_VERSION = "1.1.26"  # Ваша версия

# Обновленный маппинг дней
DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

def get_display_units(med):
    """Возвращает правильные единицы измерения (мл для жидкостей)"""
    form = med.get("form", "tablets")
    if form == "drops":
        return "капель", "капель"
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"

def parse_times(text):
    """Парсинг времени, включая поддержку 24:00 -> 00:00"""
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if hh == 24 and mm == 0: hh = 0 
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

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
        "Нажми «Начать», чтобы запустить меню 👇",
        reply_markup=start_menu() # Здесь оставляем Inline кнопку "Начать"
    )
        
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

data_store = {}
user_states = {}
started_users = set()
pending_delayed_tasks = set()
DATA_FILE = Path(os.getenv("DATA_FILE", "meds_data.json"))

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
                # Если дата без зоны, делаем ее московской
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


def save_data_store():
    # Используем глобальную переменную
    serializable = {
        str(chat_id): {name: _serialize_med(med) for name, med in meds.items()}
        for chat_id, meds in data_store.items()
    }
    # Добавим временный файл для надежности (чтобы не повредить основной при сбое)
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(DATA_FILE) # Атомарная замена


def load_data_store():
    global data_store
    if not DATA_FILE.exists():
        print(f"Файл {DATA_FILE} не найден, начинаем с пустого списка.")
        data_store = {}
        return

    try:
        raw_text = DATA_FILE.read_text(encoding="utf-8")
        if not raw_text.strip():
            data_store = {}
            return
        
        raw = json.loads(raw_text)
        data_store.clear()
        for chat_id, meds in raw.items():
            # Преобразуем ключи обратно в int
            data_store[int(chat_id)] = {name: _deserialize_med(med) for name, med in meds.items()}
        print(f"Загружено лекарств: {len(data_store)}")
    except Exception as e:
        print(f"Ошибка при загрузке данных: {e}")
        data_store = {}

# Обновленный маппинг дней
DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник", "2": "Среда", "3": "Четверг",
    "4": "Пятница", "5": "Суббота", "6": "Воскресенье"
}

# ================== МЕНЮ ==================

def start_menu():
    # Вместо "Начать" — "Запустить систему" или "Перезапустить"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Перезапустить бота", callback_data="start_bot")]])

from telegram import ReplyKeyboardMarkup # Добавьте этот импорт в начало, если его нет

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
        [InlineKeyboardButton("💨 Спрей", callback_data="form_spray")], # Добавлено
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
    
    # Групповые кнопки
    for key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"📅 {DAYS_MAP[key]}"
        if times_dict.get(key):
            text += f" ({', '.join(times_dict[key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{key}")])

    # Одиночные дни
    for i in range(7):
        day_key = str(i)
        text = DAYS_MAP[day_key]
        if times_dict.get(day_key):
            text += f" ({', '.join(times_dict[day_key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{day_key}")])

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "liquid": ("бутылке", "бутылок"),
    "drops": ("флаконе", "флаконов"),
    "spray": ("флаконе", "флаконов"), # Добавлено
}

def reminder_action_menu(med_name: str):
    """Унифицированное меню действий для напоминаний."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 10м", callback_data=f"later:10:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 20м", callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 30м", callback_data=f"later:30:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 1 час", callback_data=f"later:60:{med_name}")]
    ])

# ================== HELPERS ==================

def get_now():
    """Возвращает текущее время по Москве"""
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    # Защита от деления на ноль, если дозировка не указана
    if not med.get("daily_mg") or med["daily_mg"] <= 0:
        return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    start_dt = med["start_date"]
    now_dt = get_now()
    days_passed = (now_dt - start_dt).days
    left = capacity_days - days_passed
    return max(0, left) # Чтобы не уходило в минус

def parse_times(text):
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

def format_schedule(times_dict):
    """Форматирует расписание для вывода в текст"""
    if not isinstance(times_dict, dict):
        return "не установлено"
    if "Everyday" in times_dict and times_dict["Everyday"]:
        return f"Каждый день: {', '.join(times_dict['Everyday'])}"
    lines = []
    days_present = [k for k in times_dict.keys() if k != "Everyday" and times_dict[k]]
    sorted_days = sorted(days_present)
    for day in sorted_days:
        day_name = DAYS_MAP.get(day, day)
        lines.append(f"{day_name}: {', '.join(times_dict[day])}")
    if not lines:
        return "не установлено"
    return "\n".join(lines)

def get_display_units(med):
    """Возвращает правильные единицы измерения для текста"""
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    if form == "spray":
        return "мл", "впрыскиваний" # Добавлено
    if form == "liquid":
        return "мл", "мл" # Изменено с мг на мл
    return "мг", "мг"

# ================== START ==================

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
        reply_markup=main_menu() # Здесь теперь нижние кнопки
    )

# ================== TEXT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in {
        "старт",
        "начать",
        "start",
        "/start",
        "🚀 старт",
        "меню старт",
        "старт меню",
    }:
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await update.message.reply_text(
            f"Бот перезапущен (v{BOT_VERSION}). Главное меню:",
            reply_markup=main_menu(),
        )
        return

    # --- НОВЫЙ БЛОК: Распознавание нижних кнопок ---
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
    elif text == "🔄 Докуплено / Пополнить" or text == "♻️ Докуплено / Пополнить":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"refill:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text == "🔧 Изменить дозировку" or text == "🛠️ Изменить дозировку":
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
        await show_summary(update, context) # Вызываем вашу функцию сводки
        return
    elif text == "🗑 Удалить лекарство":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"delete:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return
    # --- КОНЕЦ НОВОГО БЛОКА ---

    # ДАЛЕЕ ВАШ ОРИГИНАЛЬНЫЙ КОД БЕЗ ИЗМЕНЕНИЙ
    state = user_states.get(chat_id)
    if not state:
        if chat_id not in started_users:
            await start(update, context)
        return
    
    d = state["data"] if "data" in state else {}

    # ---------- ADD ----------
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
            # Изменено: добавлена логика для спрея и жидкой формы
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
            if d["course_type"] == "months":
                value *= 30
            d["course_days"] = value
            await save_medicine(update, chat_id)

    # ---------- SET REMINDER (SPECIFIC DAY) ----------
    elif state["flow"] == "set_reminder":
        try:
            med_name = state["medicine"]
            day_key = state["day_key"]
            
            # Безопасное получение данных
            if chat_id not in data_store or med_name not in data_store[chat_id]:
                await update.message.reply_text("❌ Ошибка: лекарство не найдено.")
                user_states.pop(chat_id, None)
                return

            med_data = data_store[chat_id][med_name]
            # Инициализация словаря, если его нет
            if "times" not in med_data or not isinstance(med_data["times"], dict):
                med_data["times"] = {}

            if text.lower() in ["0", "нет", "удалить", "off", "выкл"]:
                if day_key in med_data["times"]:
                    del med_data["times"][day_key]
                status_msg = f"🗑 Удалено время для «{med_name}» ({DAYS_MAP.get(day_key, day_key)})"
            else:
                times = parse_times(text)
                if not times:
                    await update.message.reply_text("⚠️ Не удалось распознать время. Введите в формате ЧЧ:ММ (или '0' для удаления)")
                    return
                
                # Если выбрали "Каждый день" - очищаем всё остальное
                if day_key == "Everyday":
                    med_data["times"] = {"Everyday": times}
                else:
                    # Если настраиваем конкретный день, удаляем "Everyday"
                    if "Everyday" in med_data["times"]:
                        del med_data["times"]["Everyday"]
                    med_data["times"][day_key] = times
                
                status_msg = f"✅ Сохранено: {DAYS_MAP.get(day_key, day_key)} — {', '.join(times)}"

            times_dict = med_data["times"]
            save_data_store()
            user_states.pop(chat_id)

            await update.message.reply_text(
                f"{status_msg}\n\n"
                f"Выберите следующий день для настройки:",
                reply_markup=days_menu(med_name, times_dict)
            )
            
        except Exception as e:
            print(f"Error in set_reminder: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка. Попробуйте снова.")
            user_states.pop(chat_id, None)

    # ---------- CHANGE DOSE ----------
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

    # ---------- REFILL ----------
    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            _, plural = FORM_LABELS.get(state["form"], ("единице", "единиц"))
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text.replace(",", ".")))
            med_name = state["medicine"]
            med = data_store[chat_id][med_name]
            
            new_unit_size = state["data"]["unit_mg"]
            
            # РАСЧЕТ ДОБАВЛЕНИЯ
            if med.get("form") == "drops":
                # Переводим мл в капли
                added_resource = (new_unit_size / 0.05) * units
            elif med.get("form") == "spray":
                # Переводим мл в впрыскивания
                added_resource = (new_unit_size / 0.1) * units
            else:
                # Обычный расчет
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

            # Проверка курса
            if med["course_days"]:
                msg += f"\nДлительность курса: {med['course_days']} дней"
                needed_resource = med["course_days"] * med["daily_mg"]

                if med["total_mg"] < needed_resource:
                    deficit = needed_resource - med["total_mg"]
                    
                    if med.get("form") == "drops":
                        deficit_ml = deficit * 0.05
                        missing_units = deficit_ml / new_unit_size
                        dose_text = f"{new_unit_size:g} мл"
                    else:
                        missing_units = deficit / new_unit_size
                        dose_text = f"{new_unit_size:g} мг"

                    msg += f"\n⚠️ На курс не хватит, нужно докупить {missing_units:g} ед. при объеме/дозировке {dose_text}."
                else:
                    surplus = med["total_mg"] - needed_resource
                    if surplus > 0:
                        if med.get("form") == "drops":
                            surplus_val = surplus * 0.05 
                            surplus_units = surplus_val / new_unit_size
                        else:
                            surplus_units = surplus / new_unit_size
                        msg += f"\n✅ На курс хватит, останется излишек {surplus_units:g} ед."
                    else:
                        msg += f"\n✅ На курс хватит ровно."

            await update.message.reply_text(msg, reply_markup=main_menu())
            user_states.pop(chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "meds_info":
        await show_summary(query)
        return

    if data.startswith("taken:"):
        _, med_name = data.split(":", 1)
        await query.edit_message_text(f"✅ Прием «{med_name}» отмечен. Молодец!")
        return

    if data.startswith("later:"):
        # Формат callback_data:
        # 1) new: later:<minutes>:<med_name>
        # 2) legacy: later:<med_name>
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            minutes = int(parts[1])
            med_name = parts[2]
        else:
            minutes = 20
            med_name = parts[1] if len(parts) > 1 else ""
        
        # Считаем время
        remind_at = get_now() + timedelta(minutes=minutes)
        time_str = remind_at.strftime("%H:%M")
        
        # Создаем задание: через JobQueue, а если ее нет — через asyncio task.
        if context.job_queue:
            context.job_queue.run_once(
                send_delayed_reminder,
                when=minutes * 60,
                data={'chat_id': chat_id, 'med_name': med_name}
            )
        else:
            task = asyncio.create_task(
                send_delayed_reminder_fallback(context.bot, chat_id, med_name, minutes)
            )
            pending_delayed_tasks.add(task)
            task.add_done_callback(pending_delayed_tasks.discard)

        await query.edit_message_text(f"⏳ Хорошо, напомню про «{med_name}» через {minutes} мин. в {time_str}.")
        return

    if data == "start_bot":
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await query.message.reply_text(f"Бот перезапущен (v{BOT_VERSION}). Главное меню:", reply_markup=main_menu())
        return
        
    elif data == "main_menu":
        if chat_id in user_states:
            user_states.pop(chat_id)
        await query.message.reply_text("Возврат в главное меню:", reply_markup=main_menu())
        return

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")

    elif data.startswith("form_"):
        form = data.split("_")[1]
        user_states[chat_id]["data"]["form"] = form
        
        # Специальный вопрос для капель
        if form == "drops":
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text("Объем флакона (мл)?")
        elif form in ("spray", "liquid"):
            singular, _ = FORM_LABELS.get(form, ("единице", "единиц"))
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text(f"Сколько мл в одной {singular}?")
        else:
            singular, _ = FORM_LABELS.get(form, ("единице", "единиц"))
            user_states[chat_id]["step"] = "unit_mg"
            await query.message.reply_text(f"Сколько мг в одной {singular}?")

    elif data.startswith("course_"):
        state = user_states[chat_id]
        d = state["data"]
        
        if data.endswith("forever"):
            d["course_days"] = None
            d["course_type"] = "forever"
            await save_medicine(query, chat_id)
        else:
            d["course_type"] = data.split("_")[1]
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "start_course":
        meds = data_store.get(chat_id, {})
        not_started = [name for name, m in meds.items() if not m.get("is_started")]
        
        if not not_started:
            await query.message.reply_text("Нет лекарств для запуска или все курсы уже начаты.", reply_markup=main_menu())
            return
            
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in not_started]
        await query.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("start_now:"):
        _, med_name = data.split(":", 1) # Безопасный сплит
        med = data_store[chat_id][med_name]
        
        med["is_started"] = True
        med["start_date"] = get_now()
        save_data_store()
        
        await query.message.reply_text(f"▶️ Курс «{med_name}» начат!\nОбратный отсчет запущен.", reply_markup=main_menu())

    elif data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство для настройки расписания:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("open_days:"):
        _, med_name = data.split(":", 1) # Безопасный сплит
        
        if med_name in data_store[chat_id]:
            med_data = data_store[chat_id][med_name]
            if "times" not in med_data or not isinstance(med_data["times"], dict):
                med_data["times"] = {}
            times_dict = med_data["times"]
            await query.message.reply_text(
                f"📅 Настройка: «{med_name}».\nВыберите день недели:",
                reply_markup=days_menu(med_name, times_dict)
            )

    elif data.startswith("set_day:"):
        _, med_name, day_key = data.split(":", 2) # Безопасный сплит (3 части)
        
        user_states[chat_id] = {
            "flow": "set_reminder", 
            "medicine": med_name,
            "day_key": day_key
        }
        day_label = DAYS_MAP.get(day_key, day_key)
        await query.edit_message_text(
            f"⏰ Введите время для «{med_name}» ({day_label}).\n"
            f"Формат: 8:00, 20:00 (через запятую).\n"
            f"Отправьте «0» или «удалить», чтобы очистить день.",
            reply_markup=None
        )

    elif data in ("dose", "refill", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        # Это для dose/refill/delete
        action, med = data.split(":", 1) # Безопасный сплит
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
                "flow": "refill",
                "medicine": med,
                "step": "unit_mg",
                "form": form,
                "data": {}
            }
            if form == "drops":
                await query.message.reply_text("Объем флакона (мл)?")
            elif form in ("spray", "liquid"):
                await query.message.reply_text("Сколько мл в одной единице?")
            else:
                await query.message.reply_text("Сколько мг в одной единице?")

        elif action == "delete":
            data_store[chat_id].pop(med)
            save_data_store()
            await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

    # Внутри функции buttons найдите обработку этих callback_data:
    elif data == "meds_info":
        await show_meds_info(query)

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    unit_size = d["unit_mg"] 
    units_count = d["units"]
    form = d.get("form")
    
    if form == "drops":
        total_resource = (unit_size / 0.05) * units_count
    elif form == "spray":
        # 1 впрыскивание = 0.1 мл. Считаем общий запас впрыскиваний.
        total_resource = (unit_size / 0.1) * units_count
    else:
        # Для таблеток и жидкой формы (мл) считаем общий объем ресурса напрямую
        total_resource = unit_size * units_count

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "form": form, 
        "daily_mg": d["daily_mg"],       
        "unit_mg": d["unit_mg"],         
        "total_mg": total_resource,      
        "course_days": d.get("course_days"),
        "created": get_now(),
        "is_started": False,
        "start_date": None,
        "times": {},
        "notified": False,
    }

    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    unit_label, dose_label = get_display_units(med)
    
    # Красивое сообщение при добавлении
    msg = f"✅ Лекарство добавлено\n\nНазвание: {d['name']}\n"
    if form in ["drops", "spray", "liquid"]:
        msg += f"Объем флакона/ед: {d['unit_mg']:g} мл\n"
    else:
        msg += f"Дозировка ед: {d['unit_mg']:g} мг\n"
    
    msg += f"Расход: {d['daily_mg']:g} {dose_label}/сутки\nХватит на: {days} дней"
    msg += "\n\n⚠️ Нажмите «▶️ Начать курс» для старта отсчета."

    save_data_store()
    await update.message.reply_text(msg, reply_markup=main_menu())
    user_states.pop(chat_id)

# ================== SUMMARY / FORECAST ==================

async def show_summary(update_or_query, context: ContextTypes.DEFAULT_TYPE = None):
    # Определяем, откуда пришел вызов: из сообщения или из кнопки под сообщением
    if hasattr(update_or_query, 'message') and update_or_query.message:
        # Это обычное сообщение (нажата нижняя кнопка)
        message = update_or_query.message
        chat_id = message.chat.id
    else:
        # Это callback_query (нажата инлайн-кнопка)
        message = update_or_query.callback_query.message
        chat_id = message.chat.id

    meds = data_store.get(chat_id, {})
    if not meds:
        await message.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка и прогноз:\n\n"
    # Внутри функции show_summary замените цикл:
    for name, med in meds.items():
        days_left = calc_days_left(med)
        status = "▶️ Идет прием" if med.get("is_started") else "⏸ Ожидание старта"
        
        if not isinstance(med.get("times"), dict): med["times"] = {}
        schedule_text = format_schedule(med.get("times", {}))
        unit_label, dose_label = get_display_units(med)

        msg += f"💊 Название: {name} ({status})\n"
        
        if med.get("form") in ["drops", "spray", "liquid"]:
            msg += f"Дозировка ед: {med['unit_mg']:g} мл\n"
        else:
            msg += f"Дозировка ед: {med['unit_mg']:g} мг\n"
            
        msg += f"Время приема:\n{schedule_text}\n"

        # --- НОВЫЙ ПУНКТ: Количество приема ---
        times_dict = med.get("times", {})
        # Ищем любое заполненное расписание для примера (Каждый день -> Будни -> Выходные)
        sample_key = None
        if times_dict.get("Everyday"): sample_key = "Everyday"
        elif times_dict.get("Weekdays"): sample_key = "Weekdays"
        elif times_dict.get("Weekends"): sample_key = "Weekends"
        
        if sample_key:
            cnt = len(times_dict[sample_key])
            per_dose = med["daily_mg"] / cnt
            msg += f"Количество приема: {cnt} раза в день по {per_dose:g} {dose_label}\n"
        # ---------------------------------------

        msg += f"Хватит на: {days_left} дней при расходе {med['daily_mg']:g} {dose_label}/сутки\n"

        if med.get("course_days"):
            if med.get("is_started") and med.get("start_date"):
                # Приводим сохраненную дату к правильному формату с TZ
                start_dt = med["start_date"]
                if start_dt.tzinfo is None:
                    start_dt = TZ_MOSCOW.localize(start_dt)
                
                now = get_now()
                # Разница в полных сутках
                days_passed = (now - start_dt).days
                remaining_days = max(0, med["course_days"] - days_passed)
                
                # Дата окончания рассчитывается от даты старта
                end_date = start_dt + timedelta(days=med["course_days"])
                
                msg += f"Курс: еще {remaining_days} дн. (до {end_date.strftime('%d.%m.%Y')})\n"
                
                if days_left >= remaining_days:
                    msg += "✅ На курс хватит\n"
                else:
                    msg += "⚠️ Нужно докупить!\n"
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

# ================== REMINDER LOOP ==================

async def reminder_loop(app):
    print("Фоновый цикл напоминаний запущен!")
    while True:
        try:
            now_msk = get_now()
            current_time_str = now_msk.strftime("%H:%M")
            current_weekday = str(now_msk.weekday()) 
            is_weekend = now_msk.weekday() >= 5

            for chat_id, meds in data_store.items():
                for name, m in meds.items():
                    if not m.get("is_started"): continue

                    t_dict = m.get("times", {})
                    # Собираем все времена для текущего дня
                    times_to_check = []
                    if t_dict.get("Everyday"): times_to_check.extend(t_dict["Everyday"])
                    if is_weekend and t_dict.get("Weekends"): times_to_check.extend(t_dict["Weekends"])
                    if not is_weekend and t_dict.get("Weekdays"): times_to_check.extend(t_dict["Weekdays"])
                    if t_dict.get(current_weekday): times_to_check.extend(t_dict[current_weekday])
                    
                    times_for_today = set(times_to_check)

                    if current_time_str in times_for_today:
                        # ВАЖНО: Добавляем 'name' в ключ, чтобы не путать лекарства
                        reminder_key = f"{now_msk.date().isoformat()}:{current_time_str}:{name}"
                        
                        if m.get("last_reminder_key") == reminder_key:
                            continue

                        _, dose_label = get_display_units(m)
                        doses_count = len(times_for_today)
                        per_dose = m['daily_mg'] / doses_count if doses_count > 0 else m['daily_mg']
                        
                        keyboard = reminder_action_menu(name)

                        await app.bot.send_message(
                            chat_id,
                            f"⏰ Время принимать лекарство: {name}\n"
                            f"Дозировка: {per_dose:g} {dose_label}",
                            reply_markup=keyboard
                        )
                        m["last_reminder_key"] = reminder_key
                        save_data_store()

                    # Проверка остатков
                    if now_msk.hour == 9 and now_msk.minute == 0:
                        r_key_9am = f"{now_msk.date().isoformat()}:09am:{name}" # Добавили name
                        if m.get("last_9am_key") != r_key_9am:
                            days = calc_days_left(m)
                            if 0 < days <= 7 and not m.get("notified"):
                                await app.bot.send_message(chat_id, f"🛒 Заканчивается {name}\nХватит на: {days} дн.\nПора купить 💊")
                                m["notified"] = True
                            m["last_9am_key"] = r_key_9am
                            save_data_store()
                    
                    if now_msk.hour == 0 and now_msk.minute == 0:
                        m["notified"] = False

        except Exception as e:
            print(f"Ошибка в цикле напоминаний: {e}")
        
        await asyncio.sleep(30)

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "перезапустить бота"),
    ])
    app.create_task(reminder_loop(app))

# ... здесь заканчивается функция reminder_loop или show_forecast ...

async def send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    med_name = job.data['med_name']
    chat_id = job.data['chat_id']
    meds = data_store.get(chat_id, {})
    
    if med_name in meds:
        m = meds[med_name]
        _, dose_label = get_display_units(m)
        
        # Клавиатура с полным набором кнопок для повторных переносов
        keyboard = reminder_action_menu(med_name)
        
        await context.bot.send_message(
            chat_id,
            f"🔔 Напоминание: {med_name}\nДозировка: {m['daily_mg']:g} {dose_label}",
            reply_markup=keyboard
        )

async def send_delayed_reminder_fallback(bot, chat_id: int, med_name: str, minutes: int):
    await asyncio.sleep(minutes * 60)
    meds = data_store.get(chat_id, {})
    if med_name not in meds:
        return

    m = meds[med_name]
    _, dose_label = get_display_units(m)
    keyboard = reminder_action_menu(med_name)

    await bot.send_message(
        chat_id,
        f"🔔 Напоминание: {med_name}\nДозировка: {m['daily_mg']:g} {dose_label}",
        reply_markup=keyboard
    )

def main():
    # 1. Загружаем данные
    load_data_store()
    if not BOT_TOKEN: 
        raise RuntimeError("BOT_TOKEN не найден")
    
    # 2. Создаем приложение
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 3. Настраиваем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    # 4. Устанавливаем команды и фоновый цикл
    app.post_init = post_init
    
    # 5. Запуск бота
    print(f"Бот v{BOT_VERSION} запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
