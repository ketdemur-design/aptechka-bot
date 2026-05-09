import os
import asyncio
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pytz
from settings import APP_VERSION
from telegram import (
    ReplyKeyboardMarkup, BotCommand, Update,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ══════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════
TZ_MOSCOW       = pytz.timezone('Europe/Moscow')
BOT_VERSION     = APP_VERSION
DATA_FILE       = Path("meds_data.json")
BOT_TOKEN       = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

data_store      = {}
user_states     = {}
started_users   = set()
_write_lock     = asyncio.Lock()
bot_application = None

print(f"🤖 Бот v{BOT_VERSION}  |  данные: {DATA_FILE.absolute()}")

# ══════════════════════════════════════════════════════
#  СЕРИАЛИЗАЦИЯ / ДЕСЕРИАЛИЗАЦИЯ
# ══════════════════════════════════════════════════════
def _serialize_med(med: dict) -> dict:
    p = dict(med)
    for k in ("created", "start_date"):
        if isinstance(p.get(k), datetime):
            p[k] = p[k].isoformat()
    return p

def _deserialize_med(med: dict) -> dict:
    p = dict(med)
    for k in ("created", "start_date"):
        v = p.get(k)
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                p[k] = (dt if dt.tzinfo else TZ_MOSCOW.localize(dt)).astimezone(TZ_MOSCOW)
            except Exception:
                p[k] = None
    p.setdefault("times",             {})
    p.setdefault("notified",          False)
    p.setdefault("is_started",        False)
    p.setdefault("last_reminder_key", None)
    p.setdefault("last_9am_key",      None)
    p.setdefault("initial_units",     None)   # для % прогресса пожизненных
    return p

async def save_data_store_async():
    ser = {
        str(cid): {n: _serialize_med(m) for n, m in meds.items()}
        for cid, meds in data_store.items()
    }
    async with _write_lock:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ser, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_FILE)

def save_data_store():
    ser = {
        str(cid): {n: _serialize_med(m) for n, m in meds.items()}
        for cid, meds in data_store.items()
    }
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ser, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)

def load_data_store():
    global data_store
    if not DATA_FILE.exists():
        data_store = {}
        return
    try:
        raw = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            data_store = {}
            return
        data_store.clear()
        for cid, meds in json.loads(raw).items():
            data_store[int(cid)] = {n: _deserialize_med(m) for n, m in meds.items()}
        print(f"✅ Загружено лекарств: {sum(len(v) for v in data_store.values())}")
    except Exception as e:
        print(f"❌ Ошибка загрузки: {e}")
        data_store = {}

# ══════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РАСЧЁТОВ
# ══════════════════════════════════════════════════════
def get_now() -> datetime:
    return datetime.now(TZ_MOSCOW)

def _parse_dt(val) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val if val.tzinfo else TZ_MOSCOW.localize(val)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
            return dt if dt.tzinfo else TZ_MOSCOW.localize(dt)
        except Exception:
            return None
    return None

def calc_remaining_units(med: dict) -> float:
    """
    Остаток в штуках/единицах.
    Формула: total_mg / unit_mg
    Для drops/spray total_mg хранит объём мл — возвращаем напрямую.
    """
    form    = med.get("form", "tablets")
    total   = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return total
    return (total / unit_mg) if unit_mg > 0 else 0

def calc_daily_units(med: dict) -> float:
    """
    Суточный расход в штуках/единицах.
    Формула: daily_mg / unit_mg
    Для drops/spray daily_mg уже хранит кол-во единиц (капель/впрыскиваний).
    """
    form     = med.get("form", "tablets")
    daily_mg = med.get("daily_mg", 0) or 0
    unit_mg  = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return daily_mg
    return (daily_mg / unit_mg) if unit_mg > 0 else 0

def calc_days_left(med: dict) -> int:
    """
    Дней до исчерпания ЗАПАСА (физических таблеток/мл).
    Не зависит от длины курса — только от запаса и суточного расхода.
    Если курс начат — учитываем уже прошедшие дни.
    """
    dmg = med.get("daily_mg") or 0
    if dmg <= 0:
        return 0
    # сколько дней хватит текущего остатка при ежедневном расходе
    capacity = int((med.get("total_mg") or 0) // dmg)
    if not med.get("is_started") or not med.get("start_date"):
        return capacity
    start = _parse_dt(med["start_date"])
    if not start:
        return capacity
    passed = (get_now() - start).days
    return max(0, capacity - passed)

def calc_progress(med: dict) -> int:
    """
    Прогресс шкалы (%):
    • Курс N дней  → % прошедших дней  (2 из 10 дней = 20%).
    • Пожизненно   → % израсходованного от начального запаса упаковки.
    Если initial_units не сохранён — считаем через дни от условного горизонта.
    """
    cd = med.get("course_days") or 0

    if cd > 0:
        # ── курсовой режим: % прошедших дней курса ──
        if not med.get("is_started") or not med.get("start_date"):
            return 0
        start = _parse_dt(med["start_date"])
        if not start:
            return 0
        passed = (get_now() - start).days
        return max(0, min(100, int(passed / cd * 100)))

    else:
        # ── пожизненный: % израсходованного от начального запаса ──
        init    = med.get("initial_units")
        unit_mg = med.get("unit_mg") or 0
        total   = med.get("total_mg") or 0

        if init and init > 0 and unit_mg > 0:
            # точный расчёт через штуки
            rem_units = total / unit_mg
            return max(0, min(100, int((1 - rem_units / init) * 100)))

        # fallback через дни: прошло / (прошло + осталось) * 100
        dmg = med.get("daily_mg") or 0
        if not dmg or not med.get("is_started") or not med.get("start_date"):
            return 0
        start = _parse_dt(med["start_date"])
        if not start:
            return 0
        capacity = max(1, int(total // dmg))   # дней осталось
        passed   = (get_now() - start).days     # дней прошло
        horizon  = capacity + passed            # начальный запас в днях
        return max(0, min(100, int(passed / horizon * 100)))

def get_unit_label(form: str) -> str:
    return {
        "tablets": "табл.", "capsules": "капс.",
        "liquid":  "мл",    "drops":    "кап.",
        "spray":   "впрыск.","sachet":  "саше",
    }.get(form, "ед.")

def get_display_units(med: dict) -> tuple:
    """(краткая, полная) единица для текстовых сообщений бота."""
    form = med.get("form", "tablets")
    if form == "drops":  return "мл", "капель"
    if form == "spray":  return "мл", "впрыскиваний"
    if form == "liquid": return "мл", "мл"
    return "мг", "мг"

def format_schedule(times: dict) -> str:
    if not isinstance(times, dict) or not times:
        return "не установлено"
    if times.get("Everyday"):
        return f"Каждый день: {', '.join(times['Everyday'])}"
    dn = {"0":"Пн","1":"Вт","2":"Ср","3":"Чт","4":"Пт","5":"Сб","6":"Вс"}
    lines = [f"{dn.get(d,d)}: {', '.join(times[d])}" for d in sorted(times) if times.get(d)]
    return "\n".join(lines) if lines else "не указано"

def calc_total_from_units(form: str, unit_mg: float, units: float) -> float:
    """Перевод (единицы × размер единицы) → внутренний total_mg."""
    if form == "drops":
        return (unit_mg / 0.05) * units   # unit_mg = мл флакона, 0.05 мл = 1 капля
    if form == "spray":
        return (unit_mg / 0.1) * units    # 0.1 мл = 1 впрыскивание
    return unit_mg * units                # таблетки/капсулы/жидкость

# ══════════════════════════════════════════════════════
#  МЕНЮ-КЛАВИАТУРЫ БОТА
# ══════════════════════════════════════════════════════
DAYS_MAP = {
    "Everyday": "Каждый день",   "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Понедельник", "1": "Вторник",   "2": "Среда",
    "3": "Четверг",     "4": "Пятница",   "5": "Суббота", "6": "Воскресенье",
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
        ["➕ Добавить лекарство",   "▶️ Начать курс"],
        ["♻️ Докуплено / Пополнить","🛠️ Изменить дозировку"],
        ["⏰ Напоминание (Дни/Время)"],
        ["📋 Мои курсы и прогноз"],
        ["🗑 Удалить лекарство"],
    ], resize_keyboard=True)

def form_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💊 Таблетки",     callback_data="form_tablets"),
         InlineKeyboardButton("💊 Капсулы",      callback_data="form_capsules")],
        [InlineKeyboardButton("👁 Глазные капли", callback_data="form_drops"),
         InlineKeyboardButton("💨 Спрей",        callback_data="form_spray")],
        [InlineKeyboardButton("📦 Саше",         callback_data="form_sachet"),
         InlineKeyboardButton("🧴 Жидкая форма", callback_data="form_liquid")],
    ])

def course_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Дни",              callback_data="course_days")],
        [InlineKeyboardButton("🗓 Месяцы (30 дней)", callback_data="course_months")],
        [InlineKeyboardButton("♾ Пожизненно",        callback_data="course_forever")],
    ])

def days_menu(med_name: str, times: dict = None):
    if not isinstance(times, dict):
        times = {}
    kb = []
    for key in ["Everyday", "Weekdays", "Weekends"]:
        label = f"📅 {DAYS_MAP[key]}"
        if times.get(key):
            label += f" ({', '.join(times[key])})"
        kb.append([InlineKeyboardButton(label, callback_data=f"set_day:{med_name}:{key}")])
    for i in range(7):
        dk = str(i)
        label = DAYS_MAP[dk]
        if times.get(dk):
            label += f" ({', '.join(times[dk])})"
        kb.append([InlineKeyboardButton(label, callback_data=f"set_day:{med_name}:{dk}")])
    kb.append([InlineKeyboardButton("🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)

def reminder_action_menu(med_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил",                 callback_data=f"taken:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 10м",   callback_data=f"later:10:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 20м",   callback_data=f"later:20:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 30м",   callback_data=f"later:30:{med_name}")],
        [InlineKeyboardButton("⏰ Напомнить через 1 час", callback_data=f"later:60:{med_name}")],
    ])

# ══════════════════════════════════════════════════════
#  TELEGRAM ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    started_users.add(cid)
    await update.message.reply_text(
        f"Привет 👋  (v{BOT_VERSION})\n\n"
        "Я работаю по МСК.\n"
        "• слежу за остатками 💊\n"
        "• напоминаю о приёме ⏰\n"
        "• предупрежу о докупке за 7 дней\n\n"
        "Меню — внизу экрана 👇",
        reply_markup=main_menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid  = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in {"старт","начать","start","/start","🚀 старт","меню старт"}:
        started_users.add(cid)
        user_states.pop(cid, None)
        await update.message.reply_text(f"Бот перезапущен (v{BOT_VERSION}).", reply_markup=main_menu())
        return

    # ── кнопки главного меню ──
    if text == "➕ Добавить лекарство":
        user_states[cid] = {"flow": "add", "step": "name", "data": {}}
        await update.message.reply_text("Введите название лекарства:")
        return

    if text == "▶️ Начать курс":
        meds = data_store.get(cid, {})
        ns = [n for n, m in meds.items() if not m.get("is_started")]
        if not ns:
            await update.message.reply_text("Нет лекарств для запуска или все курсы уже начаты.")
            return
        kb = [[InlineKeyboardButton(n, callback_data=f"start_now:{n}")] for n in ns]
        await update.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text in ("♻️ Докуплено / Пополнить", "🔄 Докуплено / Пополнить"):
        meds = list(data_store.get(cid, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"refill:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text in ("🛠️ Изменить дозировку", "🔧 Изменить дозировку"):
        meds = list(data_store.get(cid, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"dose:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text == "⏰ Напоминание (Дни/Время)":
        meds = list(data_store.get(cid, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text == "📋 Мои курсы и прогноз":
        await show_summary(update, context)
        return

    if text == "🗑 Удалить лекарство":
        meds = list(data_store.get(cid, {}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет")
            return
        kb = [[InlineKeyboardButton(m, callback_data=f"delete:{m}")] for m in meds]
        await update.message.reply_text("Выберите для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # ── state-машина ──
    state = user_states.get(cid)
    if not state:
        return
    d = state.get("data", {})

    if state["flow"] == "add":
        step = state["step"]
        if step == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif step == "unit_mg":
            d["unit_mg"] = float(text.replace(",", "."))
            _, plural = FORM_LABELS.get(d["form"], ("единице", "единиц"))
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif step == "units":
            d["units"] = float(text.replace(",", "."))
            state["step"] = "daily_mg"
            form = d.get("form", "tablets")
            q = {
                "drops":  "Сколько капель в сутки назначено?",
                "spray":  "Сколько впрыскиваний в сутки назначено?",
                "liquid": "Сколько мл в сутки назначено?",
            }.get(form, "Сколько таблеток/капсул в сутки принимаете?")
            await update.message.reply_text(q)

        elif step == "daily_mg":
            # пользователь вводит в штуках — сохраняем тоже в штуках
            # преобразование в «мг» сделаем в _save_medicine
            d["daily_units"] = float(text.replace(",", "."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())

        elif step == "course_value":
            val = int(float(text.replace(",", ".")))
            if d.get("course_type") == "months":
                val *= 30
            d["course_days"] = val
            await _save_medicine(update.message, cid)
        return

    if state["flow"] == "set_reminder":
        med_name = state["medicine"]
        day_key  = state["day_key"]
        try:
            med_data = data_store[cid][med_name]
            if text.lower() in ["0", "нет", "удалить"]:
                med_data["times"].pop(day_key, None)
                msg = f"🗑 Удалено для {DAYS_MAP.get(day_key, day_key)}"
            else:
                times = _parse_times(text)
                if not times:
                    await update.message.reply_text("⚠️ Неверный формат. Пример: 08:00, 20:00")
                    return
                med_data["times"][day_key] = times
                msg = f"✅ Сохранено для {DAYS_MAP.get(day_key, day_key)}"
            await save_data_store_async()
            user_states.pop(cid, None)
            await update.message.reply_text(
                f"{msg}\nНастройте следующий день или вернитесь в меню:",
                reply_markup=days_menu(med_name, med_data["times"])
            )
        except Exception:
            await update.message.reply_text("✅ Изменения применены.")
            user_states.pop(cid, None)
        return

    if state["flow"] == "dose":
        # пользователь вводит в штуках
        med = data_store[cid][state["medicine"]]
        daily_units = float(text.replace(",", "."))
        unit_mg     = med.get("unit_mg") or 1
        med["daily_mg"] = daily_units * unit_mg
        await save_data_store_async()
        days = calc_days_left(med)
        user_states.pop(cid, None)
        await update.message.reply_text(
            f"🔧 Дозировка изменена! Теперь {daily_units} {get_unit_label(med.get('form','tablets'))}/сут. "
            f"Хватит на {days} дн.",
            reply_markup=main_menu()
        )
        return

    if state["flow"] == "refill":
        if state["step"] == "units":
            units   = float(text.replace(",", "."))
            med     = data_store[cid][state["medicine"]]
            form    = med.get("form", "tablets")
            unit_mg = med.get("unit_mg", 1)
            added   = calc_total_from_units(form, unit_mg, units)
            med["total_mg"] += added
            med["notified"]  = False
            # обновляем initial_units для % пожизненного режима
            if not med.get("course_days"):
                med["initial_units"] = (med.get("initial_units") or 0) + units
            await save_data_store_async()
            days = calc_days_left(med)
            user_states.pop(cid, None)
            # ✅ подтверждение пополнения
            await update.message.reply_text(
                f"🔄 Пополнено на {units} {get_unit_label(form)}! Хватит на {days} дн.",
                reply_markup=main_menu()
            )
            return

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    cid  = q.message.chat.id
    data = q.data

    if data == "main_menu":
        user_states.pop(cid, None)
        await q.message.reply_text("Главное меню:", reply_markup=main_menu())
        return

    if data.startswith("form_"):
        form = data.split("_", 1)[1]
        user_states[cid]["data"]["form"] = form
        user_states[cid]["step"] = "unit_mg"
        prompts = {
            "drops":  "Объём флакона (мл)?",
            "spray":  "Сколько мл в одном флаконе?",
            "liquid": "Сколько мл в одной единице?",
        }
        if form in prompts:
            await q.message.reply_text(prompts[form])
        else:
            singular, _ = FORM_LABELS.get(form, ("единице", "единиц"))
            await q.message.reply_text(f"Сколько мг в одной {singular}?")
        return

    if data.startswith("course_"):
        state = user_states[cid]
        d     = state["data"]
        ctype = data.split("_", 1)[1]
        d["course_type"] = ctype
        if ctype == "forever":
            d["course_days"] = None
            await _save_medicine(q, cid)
        else:
            state["step"] = "course_value"
            await q.message.reply_text("Введите количество:")
        return

    if data.startswith("start_now:"):
        med_name = data.split(":", 1)[1]
        med = data_store[cid][med_name]
        med["is_started"] = True
        med["start_date"] = get_now()
        save_data_store()
        await q.message.reply_text(f"▶️ Курс «{med_name}» начат!", reply_markup=main_menu())
        return

    if data.startswith("open_days:"):
        med_name = data.split(":", 1)[1]
        if med_name in data_store.get(cid, {}):
            med_data = data_store[cid][med_name]
            if not isinstance(med_data.get("times"), dict):
                med_data["times"] = {}
            await q.message.reply_text(
                f"📅 Настройка: «{med_name}». Выберите день:",
                reply_markup=days_menu(med_name, med_data["times"])
            )
        return

    if data.startswith("set_day:"):
        _, med_name, day_key = data.split(":", 2)
        user_states[cid] = {"flow": "set_reminder", "medicine": med_name, "day_key": day_key}
        await q.edit_message_text(
            f"⏰ Введите время для «{med_name}» ({DAYS_MAP.get(day_key, day_key)}).\n"
            "Формат: 8:00, 20:00 (через запятую). Отправьте «0» для удаления."
        )
        return

    if data.startswith("taken:"):
        med_name = data.split(":", 1)[1]
        med = data_store[cid][med_name]
        t_dict = med.get("times", {})
        dc = 1
        for t in t_dict.values():
            if t:
                dc = max(dc, len(t))
        per_dose        = med["daily_mg"] / dc
        med["total_mg"] = max(0, med["total_mg"] - per_dose)
        save_data_store()
        rem = round(calc_remaining_units(med), 1)
        un  = get_unit_label(med.get("form", "tablets"))
        await q.edit_message_text(f"✅ Приём «{med_name}» отмечен. Молодец!\nОстаток: {rem} {un}")
        return

    if data.startswith("later:"):
        parts    = data.split(":", 2)
        minutes  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        med_name = parts[2] if len(parts) > 2 else ""
        if context.job_queue:
            context.job_queue.run_once(
                _send_delayed_reminder,
                when=minutes * 60,
                data={"chat_id": cid, "med_name": med_name}
            )
        await q.edit_message_text(f"⏳ Напомню про «{med_name}» через {minutes} мин.")
        return

    if ":" in data:
        action, med_name = data.split(":", 1)
        if action == "dose":
            med = data_store.get(cid, {}).get(med_name, {})
            un  = get_unit_label(med.get("form", "tablets"))
            user_states[cid] = {"flow": "dose", "medicine": med_name}
            await q.message.reply_text(f"Введите новую суточную дозировку ({un}/сут):")
        elif action == "refill":
            med = data_store.get(cid, {}).get(med_name, {})
            un  = get_unit_label(med.get("form", "tablets"))
            user_states[cid] = {"flow": "refill", "medicine": med_name, "step": "units", "data": {}}
            await q.message.reply_text(f"Сколько {un} докупили?")
        elif action == "delete":
            data_store[cid].pop(med_name, None)
            save_data_store()
            await q.edit_message_text("🗑 Лекарство удалено")

# ══════════════════════════════════════════════════════
#  СОХРАНЕНИЕ НОВОГО ЛЕКАРСТВА
# ══════════════════════════════════════════════════════
async def _save_medicine(src, cid: int):
    d        = user_states[cid]["data"]
    form     = d["form"]
    unit_mg  = d["unit_mg"]
    units    = d["units"]
    # daily_units — введено пользователем в штуках
    daily_units = d.get("daily_units") or d.get("daily_mg") or 1
    course   = d.get("course_days")   # None = пожизненно

    # total_mg — внутреннее хранение (единицы × размер единицы)
    total    = calc_total_from_units(form, unit_mg, units)
    # daily_mg — внутреннее хранение
    daily_mg = daily_units * unit_mg if form not in ("drops", "spray") else daily_units

    data_store.setdefault(cid, {})[d["name"]] = {
        "form":              form,
        "daily_mg":          daily_mg,
        "unit_mg":           unit_mg,
        "total_mg":          total,
        "initial_units":     units,     # запоминаем для % пожизненного прогресса
        "course_days":       course,
        "created":           get_now(),
        "is_started":        False,
        "start_date":        None,
        "times":             {},
        "notified":          False,
        "last_reminder_key": None,
        "last_9am_key":      None,
    }

    await save_data_store_async()
    med  = data_store[cid][d["name"]]
    days = calc_days_left(med)
    un   = get_unit_label(form)

    # ✅ подтверждение добавления
    msg = (
        f"✅ Лекарство *{d['name']}* успешно добавлено!\n\n"
        f"Расход: {daily_units} {un}/сутки\n"
        f"Запас: {round(calc_remaining_units(med), 1)} {un}\n"
        f"Хватит на: {days} дней\n\n"
        f"⚠️ Нажмите «▶️ Начать курс» для старта отсчёта."
    )

    if hasattr(src, "reply_text"):
        await src.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
    else:
        await src.edit_message_text(msg, parse_mode="Markdown")
        await src.message.reply_text("Главное меню:", reply_markup=main_menu())

    user_states.pop(cid, None)

# ══════════════════════════════════════════════════════
#  СВОДКА И ПРОГНОЗ
# ══════════════════════════════════════════════════════
async def show_summary(update_or_obj, context=None):
    if hasattr(update_or_obj, "message"):
        msg_obj = update_or_obj.message
        cid     = msg_obj.chat.id
    else:
        msg_obj = update_or_obj.callback_query.message
        cid     = msg_obj.chat.id

    meds = data_store.get(cid, {})
    if not meds:
        await msg_obj.reply_text("Список лекарств пуст.", reply_markup=main_menu())
        return

    lines = ["📋 *Сводка и прогноз:*\n"]
    for name, med in meds.items():
        dl      = calc_days_left(med)
        cd      = med.get("course_days") or 0
        status  = "▶️ Идёт приём" if med.get("is_started") else "⏸ Ожидание старта"
        sched   = format_schedule(med.get("times", {}))
        un      = get_unit_label(med.get("form", "tablets"))
        rem     = round(calc_remaining_units(med), 1)
        dly     = round(calc_daily_units(med), 2)
        prog    = calc_progress(med)

        lines.append(f"💊 *{name}* ({status})")
        lines.append(f"Расписание: {sched}")
        lines.append(f"Расход: {dly} {un}/сут")
        lines.append(f"Остаток: {rem} {un}")
        lines.append(f"Прогресс: {prog}%")
        lines.append(f"Хватит запаса на: {dl} дн.")

        if cd > 0 and med.get("is_started") and med.get("start_date"):
            start = _parse_dt(med["start_date"])
            if start:
                passed   = max(0, (get_now() - start).days)
                cd_left  = max(0, cd - passed)
                end_dt   = start + timedelta(days=cd)
                enough   = dl >= cd_left
                lines.append(f"Конец курса: {end_dt.strftime('%d.%m.%Y')}")
                lines.append(f"Осталось дней курса: {cd_left}")
                lines.append(f"Запас {'✅ хватит' if enough else '❌ НЕ хватит'} до конца курса")
        else:
            lines.append("Режим: пожизненный ♾")

        lines.append("⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯")

    await msg_obj.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())

# ══════════════════════════════════════════════════════
#  ЦИКЛ НАПОМИНАНИЙ
# ══════════════════════════════════════════════════════
async def _reminder_loop():
    while True:
        try:
            now   = get_now()
            ts    = now.strftime("%H:%M")
            wd    = str(now.weekday())
            is_we = now.weekday() >= 5

            for cid, meds in list(data_store.items()):
                for name, m in list(meds.items()):
                    if not m.get("is_started"):
                        continue
                    t = m.get("times", {})
                    check = []
                    if t.get("Everyday"):              check += t["Everyday"]
                    if is_we and t.get("Weekends"):    check += t["Weekends"]
                    if not is_we and t.get("Weekdays"):check += t["Weekdays"]
                    if t.get(wd):                      check += t[wd]

                    if ts not in set(check):
                        continue
                    rkey = f"{now.date().isoformat()}:{ts}:{name}"
                    if m.get("last_reminder_key") == rkey:
                        continue

                    dc       = max(1, len(set(check)))
                    per_dose = m["daily_mg"] / dc
                    dly_u    = round(calc_daily_units(m) / dc, 2)
                    un       = get_unit_label(m.get("form", "tablets"))
                    if bot_application:
                        await bot_application.bot.send_message(
                            cid,
                            f"⏰ Время принимать: *{name}*\nДоза: {dly_u} {un}",
                            parse_mode="Markdown",
                            reply_markup=reminder_action_menu(name)
                        )
                    m["last_reminder_key"] = rkey
                    save_data_store()

                    # предупреждение о заканчивающемся запасе
                    dl = calc_days_left(m)
                    if dl <= 7 and not m.get("notified"):
                        await bot_application.bot.send_message(
                            cid,
                            f"⚠️ *{name}*: запаса осталось на {dl} дн.! Пора докупить.",
                            parse_mode="Markdown"
                        )
                        m["notified"] = True
                        save_data_store()

        except Exception as e:
            print(f"Ошибка reminder_loop: {e}")
        await asyncio.sleep(30)

async def _send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    job      = context.job
    med_name = job.data["med_name"]
    cid      = job.data["chat_id"]
    meds     = data_store.get(cid, {})
    if med_name in meds and bot_application:
        m   = meds[med_name]
        dly = round(calc_daily_units(m), 2)
        un  = get_unit_label(m.get("form", "tablets"))
        await bot_application.bot.send_message(
            cid,
            f"🔔 Напоминание: *{med_name}*\nДоза: {dly} {un}",
            parse_mode="Markdown",
            reply_markup=reminder_action_menu(med_name)
        )

def _parse_times(text: str) -> list:
    text  = text.replace("24:00", "00:00")
    clean = text.replace(",", " ").replace(";", " ").replace(".", ":").replace("\n", " ")
    found = re.findall(r'\b([0-9]{1,2})[:]([0-9]{2})\b', clean)
    res   = []
    for h, m in found:
        hh, mm = int(h), int(m)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            res.append(f"{hh:02d}:{mm:02d}")
    return sorted(set(res))

# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════
async def post_init(application):
    global bot_application
    bot_application = application
    await application.bot.set_my_commands([BotCommand("start", "перезапустить бота")])
    load_data_store()
    asyncio.create_task(_reminder_loop())
    print(f"✅ Бот v{BOT_VERSION} инициализирован.")

def main():
    app_bot = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(buttons))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print(f"🤖 Telegram бот v{BOT_VERSION} запускается...")
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
