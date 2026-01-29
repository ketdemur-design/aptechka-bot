import os
import asyncio
import re
from datetime import datetime, timedelta

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

data_store = {}
user_states = {}
started_users = set()

# Дни недели для интерфейса
DAYS_OF_WEEK = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье"
}

# ================== МЕНЮ ==================

def start_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start_bot")]])

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Докуплено / Пополнить", callback_data="refill")],
        [InlineKeyboardButton("🔧 Изменить дозировку", callback_data="dose")],
        [InlineKeyboardButton("⏰ Напоминание", callback_data="reminder_menu")],
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

def reminder_days_menu(chat_id, med_name):
    # Генерируем клавиатуру с днями недели
    keyboard = []
    med = data_store[chat_id][med_name]
    reminders = med.get("reminders", {})

    for day_num, day_name in DAYS_OF_WEEK.items():
        # Показываем установленное время, если есть
        times = sorted(reminders.get(day_num, []))
        time_str = ", ".join(times) if times else ""
        btn_text = f"{day_name}"
        if time_str:
            btn_text += f" ({time_str})"
        
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_day:{day_num}:{med_name}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu_back")])
    return InlineKeyboardMarkup(keyboard)

FORM_LABELS = {
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "sachet": ("саше", "саше"),
    "liquid": ("бутылке", "бутылок"),
}

# ================== HELPERS ==================

def calc_days_left(med):
    capacity_days = med["total_mg"] // med["daily_mg"]
    days_passed = (datetime.now() - med["created"]).days
    return int(capacity_days - days_passed)

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

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in started_users:
        await update.message.reply_text(
            "Привет 👋\n\n"
            "Я помогу:\n"
            "• следить за количеством лекарств 💊\n"
            "• учитывать разные дозировки\n"
            "• пересчитывать остатки\n"
            "• напоминать о покупке за 7 дней\n\n"
            "Нажми «Начать», чтобы запустить бота 👇",
            reply_markup=start_menu()
        )

# ================== TEXT ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.replace(",", ".").strip()

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
            d["unit_mg"] = float(text)
            _, plural = FORM_LABELS[d["form"]]
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            d["units"] = int(float(text))
            state["step"] = "daily_mg"
            await update.message.reply_text("Сколько мг в сутки принимаете?")

        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text)
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())

        elif state["step"] == "course_value":
            value = int(float(text))
            if d["course_type"] == "months":
                value *= 30
            d["course_days"] = value
            await save_medicine(update, chat_id)

    # ---------- CHANGE DOSE ----------
    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text)
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
            state["data"]["unit_mg"] = float(text)
            state["step"] = "units"
            _, plural = FORM_LABELS[state["form"]]
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif state["step"] == "units":
            units = int(float(text))
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

    # ---------- SET TIME (REMINDER) ----------
    elif state["flow"] == "set_reminder_time":
        time_text = update.message.text.strip().replace(".", ":")
        # Простая проверка формата времени HH:MM
        if not re.match(r"^\d{1,2}:\d{2}$", time_text):
            await update.message.reply_text("⚠️ Неверный формат. Введите время в формате ЧЧ:ММ (например, 09:00 или 14:30).")
            return

        # Приводим к формату HH:MM (добавляем 0, если нужно, например 9:00 -> 09:00)
        h, m = time_text.split(":")
        formatted_time = f"{int(h):02}:{int(m):02}"

        med_name = state["medicine"]
        day_num = state["day_num"]
        
        med = data_store[chat_id][med_name]
        
        # Сохраняем время
        if day_num not in med["reminders"]:
            med["reminders"][day_num] = []
        
        if formatted_time not in med["reminders"][day_num]:
            med["reminders"][day_num].append(formatted_time)
            med["reminders"][day_num].sort()

        # Предлагаем добавить еще время
        day_name = DAYS_OF_WEEK[day_num]
        kb = [
            [InlineKeyboardButton("➕ Добавить время", callback_data=f"add_time:{day_num}:{med_name}")],
            [InlineKeyboardButton("🔙 К выбору дней", callback_data=f"back_to_days:{med_name}")]
        ]
        
        await update.message.reply_text(
            f"✅ Добавлено время {formatted_time} на {day_name} для {med_name}.\n"
            f"Хотите добавить ещё время на этот день или вернуться?",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        # Сбрасываем ожидание текста, чтобы нажатие кнопок работало, но сохраняем контекст
        # (в данном случае мы ждем нажатия кнопки, если юзер напишет текст - ничего не произойдет 
        # или попадет в else, поэтому лучше сбросить step)
        state["step"] = None

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "start_bot":
        started_users.add(chat_id)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        
    elif data == "main_menu_back":
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        if chat_id in user_states:
            user_states.pop(chat_id)

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
        if data.endswith("forever"):
            state["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            state["data"]["course_type"] = data.split("_")[1]
            state["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет, добавьте их сначала.", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"rem_select:{m}")] for m in meds]
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu_back")])
        await query.message.reply_text("Выберите лекарство для настройки напоминаний:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("rem_select:"):
        med_name = data.split(":", 1)[1]
        await query.message.reply_text(
            f"Настройка напоминаний для: {med_name}\n"
            f"Выберите день недели:",
            reply_markup=reminder_days_menu(chat_id, med_name)
        )
    
    elif data.startswith("set_day:"):
        _, day_num_str, med_name = data.split(":")
        day_num = int(day_num_str)
        day_name = DAYS_OF_WEEK[day_num]
        
        user_states[chat_id] = {
            "flow": "set_reminder_time", 
            "medicine": med_name, 
            "day_num": day_num,
            "data": {}
        }
        
        await query.message.reply_text(
            f"Выбран день: {day_name}\n"
            f"Введите время (например, 09:00, 14:00, 20:30):"
        )

    elif data.startswith("add_time:"):
        _, day_num_str, med_name = data.split(":")
        day_num = int(day_num_str)
        # Повторно активируем режим ввода времени
        user_states[chat_id] = {
            "flow": "set_reminder_time", 
            "medicine": med_name, 
            "day_num": day_num,
            "data": {}
        }
        await query.message.reply_text("Введите время:")

    elif data.startswith("back_to_days:"):
        med_name = data.split(":", 1)[1]
        await query.message.reply_text(
            f"Настройка напоминаний для: {med_name}\nВыберите день:", 
            reply_markup=reminder_days_menu(chat_id, med_name)
        )

    elif data in ("dose", "refill", "delete"):
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds:
            await query.message.reply_text("Лекарств нет", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif ":" in data and not data.startswith(("rem_", "set_", "add_", "back_")):
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
        "created": datetime.now(),
        "notified": False,
        "reminders": {},  # {day_int: [time_str, ...]}
        "course_end_notified": False
    }

    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    dosage = d["unit_mg"]

    msg = (
        f"✅ Лекарство добавлено\n\n"
        f"Название: {d['name']}\n"
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

        msg += f"Название: {name}\n"
        msg += f"Дозировка: {dosage:g} мг\n"
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
    now = datetime.now()

    for name, med in meds.items():
        days_left = calc_days_left(med)
        dosage = med["unit_mg"]

        msg += f"Название: {name}\n"
        msg += f"Дозировка: {dosage:g} мг\n"
        msg += f"Хватит на: {days_left} дней\n"

        if med["course_days"]:
            course_end_date = now + timedelta(days=med["course_days"])
            date_str = course_end_date.strftime("%d.%m.%Y")
            
            msg += f"Длительность курса: {med['course_days']} дней до {date_str}\n"

            if days_left >= med["course_days"]:
                msg += "✅ На курс хватит, докупать не нужно\n"
            else:
                msg += "⚠️ На курс не хватит, нужно докупить\n"
        else:
            msg += "♾ Приём без ограничения срока\n"

        msg += "\n"

    await query.message.reply_text(msg.strip(), reply_markup=main_menu())

# ================== SCHEDULED LOOPS ==================

async def reminder_loop(app):
    """Цикл напоминаний о том, что лекарства заканчиваются (за 7 дней)"""
    while True:
        for chat_id, meds in data_store.items():
            for name, m in meds.items():
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
        await asyncio.sleep(86400) # Проверка раз в сутки

async def pill_reminder_loop(app):
    """Цикл минутных проверок для отправки напоминаний о приеме и окончании курса"""
    while True:
        now = datetime.now()
        current_day = now.weekday() # 0 = Понедельник
        current_time = now.strftime("%H:%M")

        for chat_id, meds in data_store.items():
            for name, med in meds.items():
                # 1. Проверка окончания курса
                if med["course_days"]:
                    days_passed = (now - med["created"]).days
                    # Если прошло дней >= курса, значит курс завершен
                    if days_passed >= med["course_days"]:
                        if not med.get("course_end_notified"):
                            # Отправляем сообщение об окончании курса
                            await app.bot.send_message(
                                chat_id,
                                f"Курс \"{name}\" {med['course_days']} дней {med['total_mg']:g} мг завершен"
                            )
                            med["course_end_notified"] = True
                        # Если курс завершен, напоминания о приеме не шлем
                        continue 
                
                # 2. Проверка напоминаний о приеме
                reminders = med.get("reminders", {})
                if current_day in reminders:
                    if current_time in reminders[current_day]:
                        # Чтобы не спамить в течение одной минуты, можно добавить проверку last_reminded
                        # Но для простоты используем sleep(60) в конце цикла, что в целом решает проблему
                        dosage = med["unit_mg"] # Дозировка из карточки
                        await app.bot.send_message(
                            chat_id,
                            f"Напоминание - выпейте {name} {dosage:g} мг"
                        )

        # Ждем 60 секунд до следующей проверки времени
        await asyncio.sleep(60)

async def post_init(app):
    app.create_task(reminder_loop(app))
    app.create_task(pill_reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
