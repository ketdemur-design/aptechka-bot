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

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

BOT_VERSION = "1.3.0"  # Текущая версия
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

data_store = {}
user_states = {}
snoozed_reminders = {} 

DAYS_MAP = {
    "Everyday": "Все дни",
    "Weekdays": "Будни",
    "Weekends": "Выходные",
    "0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт",
    "4": "Пт", "5": "Сб", "6": "Вс"
}

# ================== HELPERS ==================

def get_now():
    return datetime.now(TZ_MOSCOW)

def calc_days_left_in_stock(med):
    if med["daily_mg"] <= 0: return 0
    return int(med["total_mg"] // med["daily_mg"])

def calc_days_left_in_course(med):
    if not med.get("course_days"):
        return float('inf')
    if not med.get("is_started") or not med.get("start_date"):
        return med["course_days"]
    days_passed = (get_now() - med["start_date"]).days
    return max(0, med["course_days"] - days_passed)

def parse_times(text):
    text = text.replace("24:00", "00:00")
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if hh == 24: hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

def get_display_units(med):
    form = med.get("form", "tablets")
    if form in ["liquid", "drops"]:
        return "мл", "мл" if form == "liquid" else "капель"
    return "мг", "мг"

# ================== КЛАВИАТУРЫ ==================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить", callback_data="add"), InlineKeyboardButton("📋 Сводка", callback_data="summary")],
        [InlineKeyboardButton("▶️ Начать курс", callback_data="start_course"), InlineKeyboardButton("⏳ Прогноз", callback_data="forecast")],
        [InlineKeyboardButton("🔄 Пополнить", callback_data="refill"), InlineKeyboardButton("🔧 Доза", callback_data="dose")],
        [InlineKeyboardButton("⏰ Расписание", callback_data="reminder_menu"), InlineKeyboardButton("🗑 Удалить", callback_data="delete")],
    ])

def days_menu(med_name, times_dict=None):
    if not isinstance(times_dict, dict): times_dict = {}
    keyboard = []
    # Основные группы
    for key in ["Everyday", "Weekdays", "Weekends"]:
        label = f"⭐ {DAYS_MAP[key]}"
        if times_dict.get(key): label += " ✅"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"set_day:{med_name}:{key}")])
    
    # Сетка дней
    row = []
    for i in range(7):
        day_key = str(i)
        label = DAYS_MAP[day_key]
        if times_dict.get(day_key): label += "•"
        row.append(InlineKeyboardButton(label, callback_data=f"set_day:{med_name}:{day_key}"))
        if len(row) == 4 or i == 6:
            keyboard.append(row)
            row = []
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

# ================== ОБРАБОТЧИКИ КОМАНД ==================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /start"""
    user_states.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        f"💊 Бот-аптечка (v{BOT_VERSION})\n\n"
        "Я слежу за остатками ваших лекарств и напоминаю о приеме.\n"
        "Выберите действие ниже:",
        reply_markup=main_menu_keyboard()
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /summary через кнопку меню"""
    await show_summary_logic(update.effective_chat.id, context)

# ================== ОБРАБОТКА ТЕКСТА ==================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    state = user_states.get(chat_id)
    if not state: return

    if state["flow"] == "add":
        if state["step"] == "name":
            state["data"]["name"] = text
            state["step"] = "form"
            kb = [
                [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets"), InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
                [InlineKeyboardButton("📦 Саше", callback_data="form_sachet"), InlineKeyboardButton("🧴 Жидкость (мл)", callback_data="form_liquid")],
                [InlineKeyboardButton("👁 Капли", callback_data="form_drops")]
            ]
            await update.message.reply_text("Выберите форму выпуска:", reply_markup=InlineKeyboardMarkup(kb))
        elif state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            await update.message.reply_text("Сколько штук/флаконов купили?")
        elif state["step"] == "units":
            state["data"]["units"] = int(float(text.replace(",", ".")))
            state["step"] = "daily_mg"
            u = "мл" if state["data"]["form"] in ["liquid", "drops"] else "мг"
            await update.message.reply_text(f"Суточная доза ({u})?")
        elif state["step"] == "daily_mg":
            state["data"]["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            kb = [[InlineKeyboardButton("Дни", callback_data="course_days"), InlineKeyboardButton("Месяцы", callback_data="course_months")],
                  [InlineKeyboardButton("♾ Пожизненно", callback_data="course_forever")]]
            await update.message.reply_text("Длительность курса:", reply_markup=InlineKeyboardMarkup(kb))
        elif state["step"] == "course_value":
            val = int(text)
            if state["data"]["course_type"] == "months": val *= 30
            state["data"]["course_days"] = val
            await save_medicine(update, chat_id)

    elif state["flow"] == "set_reminder":
        med_name, day_key = state["medicine"], state["day_key"]
        med_data = data_store[chat_id][med_name]
        if text in ["0", "удалить"]:
            med_data["times"].pop(day_key, None)
        else:
            times = parse_times(text)
            if times: med_data["times"][day_key] = times
        user_states.pop(chat_id)
        await update.message.reply_text("✅ Расписание обновлено", reply_markup=days_menu(med_name, med_data["times"]))

    elif state["flow"] == "dose":
        data_store[chat_id][state["medicine"]]["daily_mg"] = float(text.replace(",", "."))
        user_states.pop(chat_id)
        await update.message.reply_text("✅ Доза изменена", reply_markup=main_menu_keyboard())

# ================== ОБРАБОТКА КНОПОК ==================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Чтобы кнопка не "висела" нажатой
    chat_id = query.message.chat.id
    data = query.data

    if data == "main_menu":
        user_states.pop(chat_id, None)
        await query.edit_message_text(f"Главное меню (v{BOT_VERSION}):", reply_markup=main_menu_keyboard())

    elif data == "add":
        user_states[chat_id] = {"flow": "add", "step": "name", "data": {}}
        await query.message.reply_text("Введите название лекарства:")

    elif data.startswith("form_"):
        f = data.split("_")[1]
        user_states[chat_id]["data"]["form"] = f
        user_states[chat_id]["step"] = "unit_mg"
        label = "Объем флакона (мл):" if f in ["liquid", "drops"] else "Дозировка одной шт (мг):"
        await query.message.reply_text(label)

    elif data.startswith("course_"):
        ct = data.split("_")[1]
        if ct == "forever":
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            user_states[chat_id]["data"]["course_type"] = ct
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Введите количество:")

    elif data == "start_course":
        meds = [n for n, m in data_store.get(chat_id, {}).items() if not m["is_started"]]
        if not meds: 
            await query.message.reply_text("Все курсы уже запущены.")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"start_now:{m}")] for m in meds]
        await query.message.reply_text("Запустить отсчет для:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("start_now:"):
        name = data.split(":", 1)[1]
        data_store[chat_id][name].update({"is_started": True, "start_date": get_now()})
        await query.message.reply_text(f"▶️ Курс {name} начат!", reply_markup=main_menu_keyboard())

    elif data == "reminder_menu":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await query.message.reply_text("Настройка расписания:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("open_days:"):
        name = data.split(":", 1)[1]
        await query.message.reply_text(f"Дни для {name}:", reply_markup=days_menu(name, data_store[chat_id][name]["times"]))

    elif data.startswith("set_day:"):
        _, name, day = data.split(":")
        user_states[chat_id] = {"flow": "set_reminder", "medicine": name, "day_key": day}
        await query.message.reply_text(f"Введите время для {DAYS_MAP[day]} (напр. 08:00, 20:00):")

    elif data.startswith("taken:"):
        name = data.split(":")[1]
        if chat_id in snoozed_reminders: snoozed_reminders[chat_id].pop(name, None)
        await query.edit_message_text(f"✅ {name}: принято!")

    elif data.startswith("snooze:"):
        name = data.split(":")[1]
        nxt = (get_now() + timedelta(minutes=20)).strftime("%H:%M")
        snoozed_reminders.setdefault(chat_id, {})[name] = nxt
        await query.edit_message_text(f"⏳ {name}: напомню в {nxt}")

    elif data == "summary":
        await show_summary_logic(chat_id, context)

    elif data == "forecast":
        await show_forecast_logic(chat_id, context)

    elif data == "delete":
        meds = list(data_store.get(chat_id, {}).keys())
        kb = [[InlineKeyboardButton(m, callback_data=f"del_confirm:{m}")] for m in meds]
        await query.message.reply_text("Удалить лекарство:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("del_confirm:"):
        data_store[chat_id].pop(data.split(":")[1], None)
        await query.message.reply_text("🗑 Удалено.", reply_markup=main_menu_keyboard())

# ================== ЛОГИКА И ЦИКЛЫ ==================

async def save_medicine(update, chat_id):
    d = user_states[chat_id]["data"]
    total = d["unit_mg"] * d["units"]
    if d["form"] == "drops": total = (d["unit_mg"] / 0.05) * d["units"]
    data_store.setdefault(chat_id, {})[d["name"]] = {
        "form": d["form"], "daily_mg": d["daily_mg"], "unit_mg": d["unit_mg"],
        "total_mg": total, "course_days": d["course_days"], "times": {},
        "is_started": False, "start_date": None, "notified": False
    }
    await update.effective_message.reply_text(f"✅ {d['name']} добавлено!", reply_markup=main_menu_keyboard())
    user_states.pop(chat_id)

async def show_summary_logic(chat_id, context):
    meds = data_store.get(chat_id, {})
    if not meds:
        await context.bot.send_message(chat_id, "Аптечка пуста.")
        return
    res = "📋 Сводка:\n\n"
    for n, m in meds.items():
        stock = calc_days_left_in_stock(m)
        res += f"💊 {n}: запас на {stock} дн.\n"
    res += f"\n---\n🤖 Версия: {BOT_VERSION}"
    await context.bot.send_message(chat_id, res, reply_markup=main_menu_keyboard())

async def show_forecast_logic(chat_id, context):
    meds = data_store.get(chat_id, {})
    res = "⏳ Прогноз:\n\n"
    for n, m in meds.items():
        stock, course = calc_days_left_in_stock(m), calc_days_left_in_course(m)
        if course == float('inf'): res += f"🔸 {n}: на {stock} дн.\n"
        elif stock >= course: res += f"🔸 {n}: хватит до конца курса ✅\n"
        else: res += f"🔸 {n}: ⚠️ не хватит (нужно еще на {int(course-stock)} дн.)\n"
    await context.bot.send_message(chat_id, res, reply_markup=main_menu_keyboard())

async def reminder_loop(app):
    while True:
        try:
            now = get_now()
            t_str, wd = now.strftime("%H:%M"), str(now.weekday())
            is_we = now.weekday() >= 5
            for chat_id, meds in list(data_store.items()):
                for name, m in list(meds.items()):
                    if m["is_started"]:
                        # Проверка окончания
                        if m["course_days"] and calc_days_left_in_course(m) <= 0:
                            await app.bot.send_message(chat_id, f"🎉 Курс {name} завершен!")
                            m["is_started"] = False
                            continue
                        # Сбор времени
                        targets = m["times"].get("Everyday", [])[:]
                        targets += m["times"].get("Weekends", []) if is_we else m["times"].get("Weekdays", [])
                        targets += m["times"].get(wd, [])
                        snz = snoozed_reminders.get(chat_id, {}).get(name)
                        if t_str in targets or t_str == snz:
                            if t_str == snz: snoozed_reminders[chat_id].pop(name)
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выпил(а)", callback_data=f"taken:{name}")],
                                                       [InlineKeyboardButton("⏳ Через 20 мин", callback_data=f"snooze:{name}")]])
                            await app.bot.send_message(chat_id, f"⏰ Время принять: {name}", reply_markup=kb)
                    # Покупка (09:00)
                    if t_str == "09:00" and not m["notified"]:
                        s, c = calc_days_left_in_stock(m), calc_days_left_in_course(m)
                        if 0 < s <= 7 and s < c:
                            await app.bot.send_message(chat_id, f"🛒 Заканчивается {name} (на {s} дн.). Пора купить!")
                            m["notified"] = True
                    if t_str == "00:00": m["notified"] = False
        except Exception as e: print(f"Error: {e}")
        await asyncio.sleep(60)

async def post_init(app):
    # Установка меню команд в интерфейсе Telegram
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("summary", "Сводка лекарств")
    ])
    app.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print(f"Бот запущен. Версия: {BOT_VERSION}")
    app.run_polling()

if __name__ == "__main__":
    main()
