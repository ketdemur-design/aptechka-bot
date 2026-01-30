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

# Жестко задаем часовой пояс Москвы
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
started_users = set()

# Список дней для отображения
DAYS_MAP = {
    "Everyday": "Каждый день",
    "0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт", "4": "Пт", "5": "Сб", "6": "Вс"
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
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet")],
        [InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
    ])

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни", callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")],
    ])

def days_menu(med_name):
    """Меню выбора дней недели для настройки времени"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Каждый день", callback_data=f"set_day:{med_name}:Everyday")],
        [
            InlineKeyboardButton("Пн", callback_data=f"set_day:{med_name}:0"),
            InlineKeyboardButton("Вт", callback_data=f"set_day:{med_name}:1"),
            InlineKeyboardButton("Ср", callback_data=f"set_day:{med_name}:2"),
            InlineKeyboardButton("Чт", callback_data=f"set_day:{med_name}:3"),
        ],
        [
            InlineKeyboardButton("Пт", callback_data=f"set_day:{med_name}:4"),
            InlineKeyboardButton("Сб", callback_data=f"set_day:{med_name}:5"),
            InlineKeyboardButton("Вс", callback_data=f"set_day:{med_name}:6"),
        ],
        [InlineKeyboardButton("🔙 В меню", callback_data="main_menu")]
    ])

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
}

# ================== HELPERS ==================

def get_now():
    """Возвращает текущее время по Москве"""
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    # Рассчитываем общую емкость
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    
    # Если курс еще не начат, возвращаем полный запас
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    
    # Если начат, считаем сколько дней прошло
    start_dt = med["start_date"]
    now_dt = get_now()
    
    # Разница в днях
    days_passed = (now_dt - start_dt).days
    
    # Чтобы не уйти в минус
    left = capacity_days - days_passed
    return left

def calc_surplus(med):
    if med["course_days"] is None:
        return None
    needed_mg = med["course_days"] * med["daily_mg"]
    surplus_mg = med["total_mg"] - needed_mg
    if surplus_mg <= 0:
        return None
    units = int(surplus_mg // med["unit_mg"])
    days = int(surplus_mg // med["daily_mg"])
    return units, days

def parse_times(text):
    """
    Парсит время из строки. 
    Понимает: 8:00, 08.00, 8 00, 20:30
    Разделители: запятая, пробел, точка с запятой, новая строка
    """
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    # Ищем шаблоны времени ЧЧ:ММ
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
            
    return sorted(list(set(valid_times)))

def format_schedule(times_dict):
    """Форматирует расписание для вывода в текст"""
    if not times_dict:
        return "не установлено"
    
    # Если есть настройка "Каждый день", показываем её приоритетно
    if "Everyday" in times_dict and times_dict["Everyday"]:
        return f"Каждый день: {', '.join(times_dict['Everyday'])}"
    
    # Иначе собираем по дням
    lines = []
    # Сортируем ключи (дни недели 0-6)
    sorted_days = sorted([k for k in times_dict.keys() if k != "Everyday"])
    for day in sorted_days:
        if times_dict[day]:
            day_name = DAYS_MAP.get(day, day)
            lines.append(f"{day_name}: {', '.join(times_dict[day])}")
    
    if not lines:
        return "не установлено"
    return "\n".join(lines)

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

    if chat_id not in started_users:
        await start(update, context)
        return

    state = user_states.get(chat_id)
    if not state:
        return

    d = state["data"]

    # ---------- ADD ----------
    if state["flow"] == "add":
        if state["step"] == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif state["step"] == "unit_mg":
            d["unit_mg"] = float(text.replace(",", "."))
            _, plural = FORM_LABELS[d["form"]]
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            d["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
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
            
            # Сразу сохраняем, время не спрашиваем
            await save_medicine(update, chat_id)

    # ---------- SET REMINDER (SPECIFIC DAY) ----------
    elif state["flow"] == "set_reminder":
        med_name = state["medicine"]
        day_key = state["day_key"]
        day_label = DAYS_MAP.get(day_key, day_key)
        
        # Проверка на удаление
        if text.lower() in ["0", "нет", "удалить", "off", "выкл"]:
            # Удаляем запись для этого дня
            if day_key in data_store[chat_id][med_name]["times"]:
                del data_store[chat_id][med_name]["times"][day_key]
                
            await update.message.reply_text(
                f"🔕 Напоминания для «{med_name}» ({day_label}) удалены.",
                reply_markup=days_menu(med_name)
            )
        else:
            # Парсинг времени
            times = parse_times(text)
            if not times:
                await update.message.reply_text("⚠️ Не удалось распознать время. Введите в формате ЧЧ:ММ (или '0' для удаления)")
                return
            
            # Если выбираем "Каждый день", очищаем индивидуальные дни, чтобы не было конфликтов
            if day_key == "Everyday":
                data_store[chat_id][med_name]["times"] = {"Everyday": times}
            else:
                # Если настраиваем конкретный день, удаляем "Everyday", если он был
                if "Everyday" in data_store[chat_id][med_name]["times"]:
                    del data_store[chat_id][med_name]["times"]["Everyday"]
                
                data_store[chat_id][med_name]["times"][day_key] = times
                
            await update.message.reply_text(
                f"✅ {day_label}: установлено время {', '.join(times)} для «{med_name}»", 
                reply_markup=days_menu(med_name)
            )
        
        # Мы не удаляем user_states полностью, чтобы человек мог настроить другой день
        # Просто сбрасываем шаг (но flow остается set_reminder)
        # Хотя для простоты возврата в меню кнопок достаточно просто ответа

    # ---------- CHANGE DOSE ----------
    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text.replace(",", "."))
        med["notified"] = False

        days = calc_days_left(med)
        dosage = med["unit_mg"]

        msg = (
            f"🔧 Дозировка изменена\n\n"
            f"Дозировка: {dosage:g} мг\n"
            f"Теперь хватает на: {days} дней при дозировке {dosage:g} мг."
        )

        await update.message.reply_text(msg, reply_markup=main_menu())
        user_states.pop(chat_id)

    # ---------- REFILL ----------
    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            _, plural = FORM_LABELS[state["form"]]
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text.replace(",", ".")))
            med_name = state["medicine"]
            med = data_store[chat_id][med_name]

            added_mg = units * state["data"]["unit_mg"]
            med["total_mg"] += added_mg
            med["notified"] = False

            days = calc_days_left(med)
            dosage = state["data"]["unit_mg"]

            msg = (
                f"🔄 Лекарство пополнено\n\n"
                f"Название: {med_name}\n"
                f"Дозировка: {dosage:g} мг\n"
                f"Хватит на: {days} дней"
            )

            if med["course_days"]:
                msg += f"\nДлительность курса: {med['course_days']} дней"
                needed_mg = med["course_days"] * med["daily_mg"]

                if med["total_mg"] < needed_mg:
                    deficit_mg = needed_mg - med["total_mg"]
                    missing_units = deficit_mg / dosage
                    msg += f"\n⚠️ На курс не хватит, нужно докупить {missing_units:g} ед. при дозировке {dosage:g} мг."
                else:
                    surplus_mg = med["total_mg"] - needed_mg
                    if surplus_mg > 0:
                        surplus_units = surplus_mg / dosage
                        msg += f"\n✅ На курс хватит, останется излишек {surplus_units:g} ед. при дозировке {dosage:g} мг."
                    else:
                        msg += f"\n✅ На курс хватит при дозировке {dosage:g} мг."

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
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        
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
        singular, _ = FORM_LABELS[form]
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
        med_name = data.split(":")[1]
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
        med_name = data.split(":")[1]
        # Показываем текущее расписание
        times_dict = data_store[chat_id][med_name].get("times", {})
        schedule_text = format_schedule(times_dict)
        
        await query.message.reply_text(
            f"📅 Расписание для «{med_name}»:\n\n{schedule_text}\n\nВыберите день для настройки:",
            reply_markup=days_menu(med_name)
        )

    elif data.startswith("set_day:"):
        # set_day:Aspirin:0 (или Everyday)
        _, med_name, day_key = data.split(":")
        
        user_states[chat_id] = {
            "flow": "set_reminder", 
            "medicine": med_name,
            "day_key": day_key
        }
        
        day_label = DAYS_MAP.get(day_key, day_key)
        await query.message.reply_text(
            f"⏰ Введите время приема для «{med_name}» — {day_label}.\n"
            f"Формат: 8:00, 20:00 (через запятую).\n"
            f"Напишите «0» или «удалить», чтобы очистить этот день."
        )

    elif data in ("dose", "refill", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data:
        action, med = data.split(":")
        if action == "dose":
            user_states[chat_id] = {"flow": "dose", "medicine": med, "data": {}}
            await query.message.reply_text("Введите новую суточную дозировку (мг):")

        elif action == "refill":
            form = data_store[chat_id][med].get("form", "tablets")
            user_states[chat_id] = {
                "flow": "refill",
                "medicine": med,
                "step": "unit_mg",
                "form": form,
                "data": {}
            }
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
    total_mg = d["unit_mg"] * d["units"]

    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "daily_mg": d["daily_mg"],
        "unit_mg": d["unit_mg"],
        "total_mg": total_mg,
        "course_days": d.get("course_days"),
        "created": get_now(),
        "is_started": False,
        "start_date": None,
        "times": {}, # Словарь для расписания
        "notified": False,
    }

    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    dosage = d["unit_mg"]

    msg = (
        f"✅ Лекарство добавлено\n\n"
        f"Название: {d['name']}\n"
        f"Дозировка: {dosage:g} мг\n"
        f"Хватит на: {days} дней\n\n"
        f"⚠️ Нажмите «▶️ Начать курс» для старта отсчета.\n"
        f"⏰ Для настройки времени приема нажмите «Напоминание»."
    )

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
        dosage = med["unit_mg"]
        status = "▶️ Идет прием" if med.get("is_started") else "⏸ Ожидание старта"
        
        schedule_text = format_schedule(med.get("times", {}))

        msg += f"Название: {name} ({status})\n"
        msg += f"Дозировка: {dosage:g} мг\n"
        msg += f"Время приема:\n{schedule_text}\n"
        msg += f"Хватит на: {days} дней при дозировке {dosage:g} мг\n"

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
        dosage = med["unit_mg"]
        
        msg += f"Название: {name}\n"
        msg += f"Дозировка: {dosage:g} мг\n"
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
            # День недели: 0=Пн, 6=Вс. Преобразуем в строку для ключа словаря
            current_weekday = str(now_msk.weekday()) 
            
            for chat_id, meds in data_store.items():
                for name, m in meds.items():
                    
                    # 1. Напоминание по времени (только если курс начат и есть расписание)
                    if m.get("is_started") and m.get("times"):
                        times_for_today = []
                        
                        # Если стоит "Каждый день"
                        if "Everyday" in m["times"]:
                            times_for_today = m["times"]["Everyday"]
                        # Если есть настройка для конкретного дня недели
                        elif current_weekday in m["times"]:
                            times_for_today = m["times"][current_weekday]
                            
                        if current_time_str in times_for_today:
                            await app.bot.send_message(
                                chat_id,
                                f"⏰ Время принимать лекарство: {name}\n"
                                f"Дозировка: {m['unit_mg']:g} мг"
                            )

                    # 2. Напоминание об остатках (в 09:00 МСК)
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
                                
                    # Сброс флага (в полночь)
                    if now_msk.hour == 0 and now_msk.minute == 0:
                        m["notified"] = False

        except Exception as e:
            print(f"Error in loop: {e}")
        
        await asyncio.sleep(60)

async def post_init(app):
    app.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
