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

# ... импорты ...
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

BOT_VERSION = "1.1.0" # Должно быть здесь

data_store = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Убираем проверку if chat_id not in started_users
    started_users.add(chat_id) 
    await update.message.reply_text(
        f"Привет 👋 (Версия: {BOT_VERSION})\n\n"  # Теперь версия будет видна всегда
        "Я работаю по московскому времени (MSK).\n"
        "Я помогу:\n"
        "• следить за остатками лекарств 💊\n"
        "• напоминать о приеме по времени ⏰\n\n"
        "Нажми «Начать», чтобы запустить меню 👇",
        reply_markup=start_menu()
    )
        
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

data_store = {}
user_states = {}
started_users = set()

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
    """
    Меню выбора дней недели (Вертикальный список с временем).
    """
    if not isinstance(times_dict, dict):
        times_dict = {}

    keyboard = []

    # 1. Кнопка "Каждый день"
    everyday_text = "🔄 Каждый день"
    if "Everyday" in times_dict and times_dict["Everyday"]:
        time_str = ", ".join(times_dict["Everyday"])
        everyday_text += f" ({time_str})"
    
    keyboard.append([InlineKeyboardButton(everyday_text, callback_data=f"set_day:{med_name}:Everyday")])

    # 2. Кнопки дней недели (0=Пн ... 6=Вс)
    for i in range(7):
        day_key = str(i)
        day_name = DAYS_MAP[day_key]
        
        button_text = day_name
        if day_key in times_dict and times_dict[day_key]:
            time_str = ", ".join(times_dict[day_key])
            button_text += f" ({time_str})"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_day:{med_name}:{day_key}")])

    # 3. Кнопка назад
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])

    return InlineKeyboardMarkup(keyboard)

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
    "drops": ("флаконе", "флаконов"),
}

# ================== HELPERS ==================

def get_now():
    """Возвращает текущее время по Москве"""
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    start_dt = med["start_date"]
    now_dt = get_now()
    days_passed = (now_dt - start_dt).days
    left = capacity_days - days_passed
    return left

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
    return "мг", "мг"

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in started_users:
        started_users.add(chat_id)
        await update.message.reply_text(
            "Привет 👋\n\n"
            "Я работаю по московскому времени (MSK).\n"
            "Я помогу:\n"
            "• следить за остатками лекарств 💊\n"
            "• напоминать о приеме по времени (по дням недели) ⏰\n"
            "• напоминать о покупке за 7 дней\n\n"
            "Нажми «Начать», чтобы запустить меню 👇",
            reply_markup=start_menu()
        )

# ================== TEXT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

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
            # Разный текст для капель и таблеток
            if d.get("form") == "drops":
                await update.message.reply_text("Сколько капель в сутки назначено?")
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
            else:
                # Обычный расчет
                added_resource = new_unit_size * units

            med["total_mg"] += added_resource
            med["notified"] = False

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

    if data == "start_bot":
        started_users.add(chat_id)
        # Замените строку ниже, чтобы увидеть версию в меню
        await query.message.reply_text(f"Главное меню (v{BOT_VERSION}):", reply_markup=main_menu())
        
    elif data == "main_menu":
        if chat_id in user_states:
            user_states.pop(chat_id)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())

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
            else:
                await query.message.reply_text("Сколько мг в одной единице?")

        elif action == "delete":
            data_store[chat_id].pop(med)
            await query.message.reply_text("🗑 Лекарство удалено", reply_markup=main_menu())

    elif data == "summary":
        await show_summary(query)

    elif data == "forecast":
        await show_forecast(query)

# ================== SAVE ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    
    unit_size = d["unit_mg"] 
    units_count = d["units"]
    
    if d.get("form") == "drops":
        # Переводим мл в капли (1 капля = 0.05 мл)
        total_resource = (unit_size / 0.05) * units_count
    else:
        total_resource = unit_size * units_count

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "form": d.get("form", "tablets"), 
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
    
    if d.get("form") == "drops":
        msg = (
            f"✅ Лекарство добавлено (Глазные капли)\n\n"
            f"Название: {d['name']}\n"
            f"Объем флакона: {d['unit_mg']:g} мл\n"
            f"Дозировка: {d['daily_mg']:g} капель/сутки\n"
            f"Хватит на: {days} дней"
        )
    else:
        msg = (
            f"✅ Лекарство добавлено\n\n"
            f"Название: {d['name']}\n"
            f"Дозировка: {d['unit_mg']:g} мг\n"
            f"Хватит на: {days} дней"
        )

    msg += "\n\n⚠️ Нажмите «▶️ Начать курс» для старта отсчета."

    await update.message.reply_text(msg, reply_markup=main_menu())
    user_states.pop(chat_id)

# ================== SUMMARY / FORECAST ==================

async def show_summary(query):
    meds = data_store.get(query.message.chat.id, {})
    if not meds:
        await query.message.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    msg = "📋 Сводка:\n\n"
    for name, med in meds.items():
        days = calc_days_left(med)
        status = "▶️ Идет прием" if med.get("is_started") else "⏸ Ожидание старта"
        
        if not isinstance(med.get("times"), dict): med["times"] = {}
        schedule_text = format_schedule(med.get("times", {}))
        
        unit_label, dose_label = get_display_units(med)

        msg += f"Название: {name} ({status})\n"
        if med.get("form") == "drops":
            msg += f"Объем флакона: {med['unit_mg']:g} {unit_label}\n"
        else:
            msg += f"Дозировка: {med['unit_mg']:g} {unit_label}\n"
            
        msg += f"Время приема:\n{schedule_text}\n"
        msg += f"Хватит на: {days} дней при расходе {med['daily_mg']:g} {dose_label}/сутки\n"

        if med["course_days"]:
            msg += f"Длительность курса: {med['course_days']} дней\n"
        
        msg += "\n"

    await query.message.reply_text(msg.strip(), reply_markup=main_menu())

async def show_forecast(query):
    meds = data_store.get(query.message.chat.id, {})
    if not meds:
        await query.message.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    msg = "⏳ Прогноз:\n\n"

    for name, med in meds.items():
        days_left = calc_days_left(med)
        unit_label, _ = get_display_units(med)
        
        msg += f"Название: {name}\n"
        if med.get("form") == "drops":
            msg += f"Объем: {med['unit_mg']:g} {unit_label}\n"
        else:
            msg += f"Дозировка: {med['unit_mg']:g} {unit_label}\n"
            
        msg += f"Хватит на: {days_left} дней\n"

        if med["course_days"]:
            if med.get("is_started") and med.get("start_date"):
                course_end_date = med["start_date"] + timedelta(days=med["course_days"])
                date_str = course_end_date.strftime("%d.%m.%Y")
                msg += f"Длительность курса: {med['course_days']} дней до {date_str}\n"
            else:
                msg += f"Длительность курса: {med['course_days']} дней (курс не начат)\n"

            if days_left >= med["course_days"]:
                msg += "✅ На курс хватит, докупать не нужно\n"
            else:
                msg += "⚠️ На курс не хватит, нужно докупить\n"
        else:
            msg += "♾ Приём без ограничения срока\n"

        msg += "\n"

    await query.message.reply_text(msg.strip(), reply_markup=main_menu())

# ================== REMINDER LOOP ==================

async def reminder_loop(app):
    while True:
        try:
            now_msk = get_now()
            current_time_str = now_msk.strftime("%H:%M")
            current_weekday = str(now_msk.weekday()) 
            
            for chat_id, meds in data_store.items():
                for name, m in meds.items():
                    if not isinstance(m.get("times"), dict): continue

                    # 1. Время приема
                    if m.get("is_started") and m.get("times"):
                        times_for_today = []
                        if "Everyday" in m["times"]:
                            times_for_today = m["times"]["Everyday"]
                        elif current_weekday in m["times"]:
                            times_for_today = m["times"][current_weekday]
                            
                        if current_time_str in times_for_today:
                            dose_text = f"{m['daily_mg']:g}"
                            if m.get("form") == "drops":
                                dose_text += " капель"
                            else:
                                dose_text += " мг"

                            await app.bot.send_message(
                                chat_id,
                                f"⏰ Время принимать лекарство: {name}\n"
                                f"Дозировка (суточная): {dose_text}"
                            )

                    # 2. Остатки (09:00)
                    if now_msk.hour == 9 and now_msk.minute == 0:
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
                    
                    if now_msk.hour == 0 and now_msk.minute == 0:
                        m["notified"] = False

        except Exception as e:
            print(f"Error in loop: {e}")
        
        await asyncio.sleep(60)

async def post_init(app):
    app.create_task(reminder_loop(app))

# ... здесь заканчивается функция reminder_loop или show_forecast ...

async def send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Эта функция вызывается через 20 минут после нажатия кнопки 'Напомнить позже'"""
    job = context.job
    med_name = job.data['med_name']
    chat_id = job.data['chat_id']
    
    meds = data_store.get(chat_id, {})
    if med_name in meds:
        m = meds[med_name]
        # Используем обновленную функцию получения единиц (мл или мг)
        unit_label, _ = get_display_units(m)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{med_name}")]])
        
        await context.bot.send_message(
            chat_id, 
            f"🔔 Повторное напоминание: {med_name}\nДозировка: {m['daily_mg']:g} {unit_label}",
            reply_markup=keyboard
        )

# Вот ваша существующая функция main, она остается в самом низу
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # ... и так далее

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
