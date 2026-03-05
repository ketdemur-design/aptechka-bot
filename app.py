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

# 7. Возможность указания версии бота
BOT_VERSION = "3"

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
started_users = set()

# 5. Изменить кнопки напоминаний (добавлены группы)
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
    if not isinstance(times_dict, dict):
        times_dict = {}
    keyboard = []

    # Групповые кнопки
    for group_key in ["Everyday", "Weekdays", "Weekends"]:
        text = f"🔄 {DAYS_MAP[group_key]}"
        if group_key in times_dict and times_dict[group_key]:
            text += f" ({', '.join(times_dict[group_key])})"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"set_day:{med_name}:{group_key}")])

    # Одиночные дни
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
    "liquid": ("бутылке", "бутылок"),
    "drops": ("флаконе", "флаконов"),
}

# ================== HELPERS ==================

def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    start_dt = med["start_date"]
    now_dt = get_now()
    days_passed = (now_dt - start_dt).days
    left = capacity_days - days_passed
    return max(0, left)

def parse_times(text):
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        # 6. Напоминание на 24:00 (превращаем в 00:00)
        if hh == 24 and mm == 0:
            hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

def format_schedule(times_dict):
    if not isinstance(times_dict, dict) or not times_dict:
        return "не установлено"
    lines = []
    for k, v in times_dict.items():
        if v:
            lines.append(f"{DAYS_MAP.get(k, k)}: {', '.join(v)}")
    return "\n".join(lines) if lines else "не установлено"

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    # 4. Перевести для жидких продуктов мг в мл
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"

# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in started_users:
        started_users.add(chat_id)
        await update.message.reply_text(
            f"Привет 👋 (v{BOT_VERSION})\n\n"
            "Я работаю по московскому времени (MSK).\n"
            "Я помогу:\n"
            "• следить за остатками лекарств 💊\n"
            "• напоминать о приеме по времени ⏰\n"
            "• напоминать о покупке только если это необходимо\n\n"
            "Нажми «Начать», чтобы запустить меню 👇",
            reply_markup=start_menu()
        )

# ================== TEXT HANDLER ==================

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
            unit_label, dose_label = ("мл", "капель") if d.get("form") == "drops" else (("мл", "мл") if d.get("form") == "liquid" else ("мг", "мг"))
            await update.message.reply_text(f"Сколько {dose_label} в сутки назначено?")
        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())
        elif state["step"] == "course_value":
            val = int(float(text.replace(",", ".")))
            if d.get("course_type") == "months": val *= 30
            d["course_days"] = val
            await save_medicine(update, chat_id)

    elif state["flow"] == "set_reminder":
        med_name = state["medicine"]
        day_key = state["day_key"]
        med_data = data_store[chat_id][med_name]
        if "times" not in med_data: med_data["times"] = {}
        
        if text.lower() in ["0", "нет", "удалить", "off"]:
            med_data["times"].pop(day_key, None)
            status_msg = f"🗑 Удалено для {DAYS_MAP.get(day_key)}"
        else:
            times = parse_times(text)
            if not times:
                await update.message.reply_text("⚠️ Формат ЧЧ:ММ")
                return
            med_data["times"][day_key] = times
            status_msg = f"✅ {DAYS_MAP.get(day_key)}: {', '.join(times)}"
        
        user_states.pop(chat_id)
        await update.message.reply_text(f"{status_msg}\nСледующий день:", reply_markup=days_menu(med_name, med_data["times"]))

    elif state["flow"] == "dose":
        med = data_store[chat_id][state["medicine"]]
        med["daily_mg"] = float(text.replace(",", "."))
        med["notified"] = False
        await update.message.reply_text("🔧 Дозировка изменена", reply_markup=main_menu())
        user_states.pop(chat_id)

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            _, plural = FORM_LABELS.get(state["form"], ("ед.", "ед."))
            await update.message.reply_text(f"Сколько {plural} купили?")
        elif state["step"] == "units":
            units = int(float(text.replace(",", ".")))
            med = data_store[chat_id][state["medicine"]]
            u_size = state["data"]["unit_mg"]
            added = (u_size / 0.05 * units) if med["form"] == "drops" else (u_size * units)
            med["total_mg"] += added
            med["notified"] = False
            await update.message.reply_text("🔄 Пополнено", reply_markup=main_menu())
            user_states.pop(chat_id)

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    data = query.data
    await query.answer()

    if data == "start_bot" or data == "main_menu":
        user_states.pop(chat_id, None)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Название лекарства:")

    elif data.startswith("form_"):
        user_states[chat_id]["data"]["form"] = data.split("_")[1]
        user_states[chat_id]["step"] = "unit_mg"
        label = "Объем (мл)?" if "drops" in data or "liquid" in data else "Мг в 1 ед.?"
        await query.message.reply_text(label)

    elif data.startswith("course_"):
        if "forever" in data:
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            user_states[chat_id]["data"]["course_type"] = data.split("_")[1]
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Количество:")

    elif data == "start_course":
        meds = [n for n, m in data_store.get(chat_id, {}).items() if not m.get("is_started")]
        if not meds: 
            await query.message.reply_text("Нет новых курсов", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in meds]
        await query.message.reply_text("Запустить курс:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("start_now:"):
        m_name = data.split(":", 1)[1]
        data_store[chat_id][m_name].update({"is_started": True, "start_date": get_now()})
        await query.message.reply_text(f"▶️ Курс «{m_name}» запущен", reply_markup=main_menu())

    elif data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("open_days:"):
        m_name = data.split(":", 1)[1]
        times = data_store[chat_id][m_name].get("times", {})
        await query.message.reply_text(f"📅 Настройка: {m_name}", reply_markup=days_menu(m_name, times))

    elif data.startswith("set_day:"):
        _, m_name, d_key = data.split(":", 2)
        user_states[chat_id] = {"flow": "set_reminder", "medicine": m_name, "day_key": d_key}
        await query.edit_message_text(f"⏰ Время для {DAYS_MAP[d_key]} (ЧЧ:ММ):")

    elif data in ["dose", "refill", "delete"]:
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"act_{data}:{m}")] for m in meds]
        await query.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("act_"):
        parts = data.split(":", 1)
        action = parts[0].replace("act_", "")
        m_name = parts[1]
        if action == "delete":
            data_store[chat_id].pop(m_name, None)
            await query.message.reply_text("🗑 Удалено", reply_markup=main_menu())
        elif action == "dose":
            user_states[chat_id] = {"flow": "dose", "medicine": m_name}
            await query.message.reply_text("Новая дозировка в сутки:")
        elif action == "refill":
            med = data_store[chat_id][m_name]
            user_states[chat_id] = {"flow": "refill", "medicine": m_name, "step": "unit_mg", "form": med["form"], "data": {}}
            await query.message.reply_text("Объем/дозировка новой единицы?")

    elif data == "summary": await show_summary(query)
    elif data == "forecast": await show_forecast(query)
    
    # 3. Обработка кнопок уведомления
    elif data.startswith("taken:"):
        await query.edit_message_text(f"✅ {query.message.text}\n\nОтмечено: принято.")
    elif data.startswith("snooze:"):
        m_name = data.split(":", 1)[1]
        context.job_queue.run_once(send_snoozed_notif, 1200, data={"chat_id": chat_id, "med_name": m_name})
        await query.edit_message_text(f"⏳ {query.message.text}\n\nНапомню через 20 минут.")

# ================== SAVE / SHOW ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    total = (d["unit_mg"] / 0.05 * d["units"]) if d["form"] == "drops" else (d["unit_mg"] * d["units"])
    data_store.setdefault(chat_id, {})
    data_store[chat_id][d["name"]] = {
        "form": d["form"], "daily_mg": d["daily_mg"], "unit_mg": d["unit_mg"],
        "total_mg": total, "course_days": d.get("course_days"),
        "created": get_now(), "is_started": False, "start_date": None,
        "times": {}, "notified": False
    }
    await update.message.reply_text(f"✅ Добавлено: {d['name']}\nНажми «Начать курс».", reply_markup=main_menu())
    user_states.pop(chat_id)

async def show_summary(query):
    meds = data_store.get(query.message.chat.id, {})
    if not meds: 
        await query.message.reply_text("Пусто", reply_markup=main_menu())
        return
    res = "📋 Сводка:\n\n"
    for n, m in meds.items():
        u, d_u = get_display_units(m)
        res += f"{n}: {calc_days_left(m)} дн. (расход {m['daily_mg']:g} {d_u})\nРасписание:\n{format_schedule(m.get('times'))}\n\n"
    await query.message.reply_text(res, reply_markup=main_menu())

async def show_forecast(query):
    meds = data_store.get(query.message.chat.id, {})
    if not meds: return
    res = "⏳ Прогноз:\n\n"
    for n, m in meds.items():
        left = calc_days_left(m)
        res += f"{n}: хватит на {left} дн.\n"
        if m["course_days"]:
            res += f"Курс: {m['course_days']} дн. {'(хватит)' if left >= m['course_days'] else '(не хватит)'}\n"
        res += "\n"
    await query.message.reply_text(res, reply_markup=main_menu())

# ================== LOOP & NOTIFS ==================

async def send_snoozed_notif(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id, m_name = job.data["chat_id"], job.data["med_name"]
    if chat_id in data_store and m_name in data_store[chat_id]:
        med = data_store[chat_id][m_name]
        _, d_u = get_display_units(med)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{m_name}")],
            [InlineKeyboardButton("⏳ Через 20 мин", callback_data=f"snooze:{m_name}")]
        ])
        # 2. Не надо писать суточную дозировку
        await context.bot.send_message(chat_id, f"⏰ ПОВТОР: {m_name}\nДозировка: {med['daily_mg']:g} {d_u}", reply_markup=kb)

async def reminder_loop(app):
    while True:
        try:
            now = get_now()
            t_str, wd = now.strftime("%H:%M"), str(now.weekday())
            for c_id, meds in data_store.items():
                for n, m in meds.items():
                    # Прием
                    if m.get("is_started") and m.get("times"):
                        sched = m["times"]
                        active_times = []
                        if "Everyday" in sched: active_times = sched["Everyday"]
                        elif "Weekdays" in sched and int(wd) < 5: active_times = sched["Weekdays"]
                        elif "Weekends" in sched and int(wd) >= 5: active_times = sched["Weekends"]
                        elif wd in sched: active_times = sched[wd]

                        if t_str in active_times:
                            _, d_u = get_display_units(m)
                            kb = InlineKeyboardMarkup([
                                [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{n}")],
                                [InlineKeyboardButton("⏳ Через 20 мин", callback_data=f"snooze:{n}")]
                            ])
                            # 2. Не надо писать суточную дозировку
                            await app.bot.send_message(c_id, f"⏰ Время принимать: {n}\nДозировка: {m['daily_mg']:g} {d_u}", reply_markup=kb)

                    # 1. Логика уведомлений о покупке и завершении курса
                    if now.hour == 9 and now.minute == 0:
                        days_left = calc_days_left(m)
                        if m.get("course_days") and m.get("is_started"):
                            days_passed = (now - m["start_date"]).days
                            course_remains = m["course_days"] - days_passed
                            
                            if course_remains <= 0:
                                await app.bot.send_message(c_id, f"✅ Курс лекарства {n} завершен!")
                                # Чтобы не спамить, можно либо удалить, либо пометить
                                m["course_days"] = None 
                            elif days_left < course_remains and days_left <= 7:
                                if not m["notified"]:
                                    await app.bot.send_message(c_id, f"🛒 Заканчивается {n}\nХватит на: {days_left} дн.\nПора купить 💊")
                                    m["notified"] = True
                        elif days_left <= 7 and not m.get("course_days"):
                            # Для пожизненных
                            if not m["notified"] and days_left > 0:
                                await app.bot.send_message(c_id, f"🛒 Заканчивается {n}\nХватит на: {days_left} дн.\nПора купить 💊")
                                m["notified"] = True

                    if now.hour == 0 and now.minute == 0: m["notified"] = False
        except: pass
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
