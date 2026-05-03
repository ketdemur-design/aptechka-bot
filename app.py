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
BOT_VERSION = "1.1.26"
# Путь для работы в Docker (совместимость с PWA)
DATA_FILE = Path(os.getenv("DATA_FILE", "/data/meds_data.json"))

data_store = {}
user_states = {}
started_users = set()
pending_delayed_tasks = set()

# ================== СЕРВИСНЫЕ ФУНКЦИИ ДАННЫХ ==================

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
            except:
                payload[dt_key] = None
    payload.setdefault("times", {})
    payload.setdefault("notified", False)
    payload.setdefault("is_started", False)
    payload.setdefault("last_reminder_key", None)
    return payload

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
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data_store.clear()
        for chat_id, meds in raw.items():
            data_store[int(chat_id)] = {name: _deserialize_med(med) for name, med in meds.items()}
    except:
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
    "tablets": ("таблетке", "таблеток"),
    "capsules": ("капсуле", "капсул"),
    "liquid": ("бутылке", "бутылок"),
    "drops": ("флаконе", "флаконов"),
    "spray": ("флаконе", "флаконов"),
    "sachet": ("саше", "саше"),
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
        [InlineKeyboardButton("💊 Таблетки", callback_data="form_tablets"), InlineKeyboardButton("💊 Капсулы", callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops"), InlineKeyboardButton("💨 Спрей", callback_data="form_spray")],
        [InlineKeyboardButton("📦 Саше", callback_data="form_sachet"), InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
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
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu_back")])
    return InlineKeyboardMarkup(keyboard)

def reminder_action_menu(med_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 20м", callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 1 час", callback_data=f"later:60:{med_name}")]
    ])

# ================== ЛОГИКА И ХЕЛПЕРЫ ==================

def get_now(): return datetime.now(TZ_MOSCOW)

def calc_days_left(med):
    if not med.get("daily_mg") or med["daily_mg"] <= 0: return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    start_dt = med["start_date"]
    days_passed = (get_now() - start_dt).days
    return max(0, capacity_days - days_passed)

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops": return "мл", "капель"
    if form == "spray": return "мл", "впрыскиваний"
    if form == "liquid": return "мл", "мл"
    return "мг", "мг"

def parse_times(text):
    text = text.replace("24:00", "00:00")
    clean_text = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    times = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean_text)
    valid_times = []
    for h, m in times:
        hh, mm = int(h), int(m)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid_times.append(f"{hh:02d}:{mm:02d}")
    return sorted(list(set(valid_times)))

# ================== ОБРАБОТЧИКИ ТЕКСТА ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    started_users.add(chat_id)
    await update.message.reply_text(
        f"Привет 👋 (v{BOT_VERSION})\nЯ помогу следить за лекарствами.\nМеню управления — внизу 👇",
        reply_markup=main_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in {"старт", "/start", "🚀 перезапустить бота"}:
        started_users.add(chat_id)
        user_states.pop(chat_id, None)
        await start(update, context)
        return

    # Нижние кнопки
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
        await show_summary(update, context)
        return
    elif text == "🗑 Удалить лекарство":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"delete:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text == "⏰ Напоминание (Дни/Время)":
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await update.message.reply_text("Настройка расписания:", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text in ["♻️ Докуплено / Пополнить", "🔄 Докуплено / Пополнить"]:
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"refill:{m}")] for m in meds]
        await update.message.reply_text("Что пополняем?", reply_markup=InlineKeyboardMarkup(kb))
        return
    elif text in ["🛠️ Изменить дозировку", "🔧 Изменить дозировку"]:
        meds = list(data_store.get(chat_id, {}).keys())
        if not meds: return
        kb = [[InlineKeyboardButton(m, callback_data=f"dose:{m}")] for m in meds]
        await update.message.reply_text("Где меняем дозировку?", reply_markup=InlineKeyboardMarkup(kb))
        return

    # Логика ввода данных (flow)
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
            u_label, _ = get_display_units(d)
            await update.message.reply_text(f"Сколько {u_label} в СУТКИ назначено?")
        elif state["step"] == "daily_mg":
            d["daily_mg"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())
        elif state["step"] == "course_value":
            val = int(float(text.replace(",", ".")))
            if d.get("course_type") == "months": val *= 30
            d["course_days"] = val
            await save_medicine(update, chat_id)

    elif state["flow"] == "dose":
        data_store[chat_id][state["medicine"]]["daily_mg"] = float(text.replace(",", "."))
        save_data_store()
        user_states.pop(chat_id)
        await update.message.reply_text("🔧 Дозировка изменена", reply_markup=main_menu())

    elif state["flow"] == "refill":
        if state["step"] == "unit_mg":
            state["data"]["unit_mg"] = float(text.replace(",", "."))
            state["step"] = "units"
            await update.message.reply_text("Сколько штук/флаконов добавлено?")
        elif state["step"] == "units":
            units = int(text)
            med = data_store[chat_id][state["medicine"]]
            added = state["data"]["unit_mg"] * units
            if med["form"] == "drops": added = (state["data"]["unit_mg"] / 0.05) * units
            elif med["form"] == "spray": added = (state["data"]["unit_mg"] / 0.1) * units
            med["total_mg"] += added
            med["notified"] = False
            save_data_store()
            user_states.pop(chat_id)
            await update.message.reply_text("🔄 Пополнено!", reply_markup=main_menu())

    elif state["flow"] == "set_reminder":
        med_name, day_key = state["medicine"], state["day_key"]
        if text.lower() in ["0", "удалить", "нет"]:
            data_store[chat_id][med_name]["times"].pop(day_key, None)
            msg = "🗑 Удалено."
        else:
            times = parse_times(text)
            if not times:
                await update.message.reply_text("⚠️ Ошибка. Пример: 8:00, 20:00")
                return
            data_store[chat_id][med_name]["times"][day_key] = times
            msg = f"✅ Сохранено для {DAYS_MAP.get(day_key, day_key)}"
        save_data_store()
        user_states.pop(chat_id)
        await update.message.reply_text(msg, reply_markup=days_menu(med_name, data_store[chat_id][med_name]["times"]))

# ================== ОБРАБОТЧИКИ КНОПОК (CALLBACKS) ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == "main_menu_back":
        user_states.pop(chat_id, None)
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        return

    if data.startswith("form_"):
        user_states[chat_id]["data"]["form"] = data.split("_")[1]
        user_states[chat_id]["step"] = "unit_mg"
        f = user_states[chat_id]["data"]["form"]
        label = "Объем флакона (мл)?" if f in ["drops", "spray", "liquid"] else "Сколько мг в одной таблетке/капсуле?"
        await query.message.reply_text(label)
    elif data.startswith("course_"):
        ctype = data.split("_")[1]
        user_states[chat_id]["data"]["course_type"] = ctype
        if ctype == "forever":
            user_states[chat_id]["data"]["course_days"] = None
            await save_medicine(query, chat_id)
        else:
            user_states[chat_id]["step"] = "course_value"
            await query.message.reply_text("Введите количество:")
    elif data.startswith("start_now:"):
        name = data.split(":")[1]
        data_store[chat_id][name].update({"is_started": True, "start_date": get_now()})
        save_data_store()
        await query.edit_message_text(f"▶️ Курс «{name}» запущен!")
    elif data.startswith("open_days:"):
        name = data.split(":")[1]
        await query.message.reply_text(f"📅 Настройка: {name}", reply_markup=days_menu(name, data_store[chat_id][name]["times"]))
    elif data.startswith("set_day:"):
        _, name, day = data.split(":")
        user_states[chat_id] = {"flow": "set_reminder", "medicine": name, "day_key": day}
        await query.message.reply_text(f"⏰ Время для {DAYS_MAP[day]} (напр. 8:00, 20:00):")
    elif data.startswith("taken:"):
        name = data.split(":")[1]
        await query.edit_message_text(f"✅ Прием «{name}» подтвержден!")
    elif data.startswith("later:"):
        parts = data.split(":")
        await query.edit_message_text(f"⏳ Хорошо, напомню про «{parts[2]}» через {parts[1]} мин.")
    elif data.startswith("delete:"):
        name = data.split(":")[1]
        data_store[chat_id].pop(name, None)
        save_data_store()
        await query.edit_message_text(f"🗑 Удалено: {name}")
    elif data.startswith("refill:"):
        user_states[chat_id] = {"flow": "refill", "medicine": data.split(":")[1], "step": "unit_mg", "data": {}}
        await query.message.reply_text("Объем/дозировка новой упаковки?")
    elif data.startswith("dose:"):
        user_states[chat_id] = {"flow": "dose", "medicine": data.split(":")[1]}
        await query.message.reply_text("Введите новую суточную дозу:")

# ================== ЛОГИКА СОХРАНЕНИЯ И СИСТЕМА ==================

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
    msg = f"✅ {d['name']} успешно добавлено!"
    if hasattr(update_or_query, 'message') and update_or_query.message: 
        await update_or_query.message.reply_text(msg, reply_markup=main_menu())
    else: 
        await update_or_query.edit_message_text(msg)
    user_states.pop(chat_id)

async def show_summary(update, context):
    chat_id = update.effective_chat.id
    meds = data_store.get(chat_id, {})
    if not meds:
        await update.message.reply_text("Ваша аптечка пуста.")
        return
    res = "📋 Сводка и прогноз:\n\n"
    for name, med in meds.items():
        days = calc_days_left(med)
        status = "▶️ Прием идет" if med.get("is_started") else "⏸ Ожидание"
        res += f"💊 *{name}* ({status})\n└ Хватит на {days} дн.\n"
        if med.get("course_days"):
            res += f"└ Курс: {med['course_days']} дн.\n"
        res += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    await update.message.reply_text(res, parse_mode="Markdown")

async def reminder_loop(application):
    while True:
        try:
            now = get_now()
            t_str = now.strftime("%H:%M")
            wd = str(now.weekday())
            is_we = now.weekday() >= 5
            for chat_id, meds in data_store.items():
                for name, m in meds.items():
                    if not m.get("is_started"): continue
                    targets = m.get("times", {}).get("Everyday", [])[:]
                    if is_we: targets += m.get("times", {}).get("Weekends", [])
                    else: targets += m.get("times", {}).get("Weekdays", [])
                    targets += m.get("times", {}).get(wd, [])
                    if t_str in targets:
                        rem_key = f"{now.date().isoformat()}:{t_str}:{name}"
                        if m.get("last_reminder_key") != rem_key:
                            await application.bot.send_message(chat_id, f"⏰ Пора принять {name}", reply_markup=reminder_action_menu(name))
                            m["last_reminder_key"] = rem_key
                            save_data_store()
        except: pass
        await asyncio.sleep(30)

async def post_init(application):
    await application.bot.set_my_commands([BotCommand("start", "Запустить бота")])
    load_data_store()
    asyncio.create_task(reminder_loop(application))

def main():
    token = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
