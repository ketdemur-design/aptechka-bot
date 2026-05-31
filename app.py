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
DATA_FILE       = Path(os.getenv("DATA_FILE", "meds_data.json"))
BOT_TOKEN       = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

data_store      = {}
user_states     = {}
started_users   = set()
_write_lock     = asyncio.Lock()
bot_application = None
_last_data_mtime = None

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
    p.setdefault("initial_units",     None)
    p.setdefault("taken_doses",       0)
    return p

async def save_data_store_async():
    """Асинхронное сохранение с объединением данных диска."""
    async with _write_lock:
        disk_data = {}
        if DATA_FILE.exists():
            try:
                raw = DATA_FILE.read_text(encoding="utf-8").strip()
                if raw:
                    disk_data = json.loads(raw)
            except Exception as e:
                print(f"Error reading before save: {e}")

        for cid, meds in data_store.items():
            str_cid = str(cid)
            disk_data.setdefault(str_cid, {})
            for name, med in meds.items():
                disk_data[str_cid][name] = _serialize_med(med)

        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(disk_data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_FILE)

def save_data_store():
    """Синхронное сохранение с объединением данных диска."""
    disk_data = {}
    if DATA_FILE.exists():
        try:
            raw = DATA_FILE.read_text(encoding="utf-8").strip()
            if raw:
                disk_data = json.loads(raw)
        except Exception as e:
            print(f"Error reading before save: {e}")

    for cid, meds in data_store.items():
        str_cid = str(cid)
        disk_data.setdefault(str_cid, {})
        for name, med in meds.items():
            disk_data[str_cid][name] = _serialize_med(med)

    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(disk_data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)

def load_data_store():
    """Загружает DATA_FILE с диска без сброса кеша при временной ошибке чтения.

    Веб-сервер сохраняет данные через запись во временный файл и атомарный replace(),
    поэтому бот читает либо старую, либо новую целую версию файла. Если файл в момент
    чтения недоступен или JSON ещё не готов, оставляем текущий data_store в памяти.
    """
    global data_store, _last_data_mtime
    if not DATA_FILE.exists():
        data_store = {}
        _last_data_mtime = None
        return
    try:
        raw = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            data_store = {}
            _last_data_mtime = DATA_FILE.stat().st_mtime if DATA_FILE.exists() else None
            return

        loaded = {}
        for cid, meds in json.loads(raw).items():
            try:
                loaded[int(cid)] = {n: _deserialize_med(m) for n, m in meds.items()}
            except (TypeError, ValueError):
                continue
        data_store.clear()
        data_store.update(loaded)
        _last_data_mtime = DATA_FILE.stat().st_mtime if DATA_FILE.exists() else None
        print(f"✅ Загружено лекарств: {sum(len(v) for v in data_store.values())}")
    except Exception as e:
        print(f"❌ Ошибка загрузки: {e}")

def refresh_data_store_if_changed():
    """Подтягивает изменения из DATA_FILE, если файл изменился во внешнем процессе (web/API)."""
    global _last_data_mtime
    if not DATA_FILE.exists():
        return
    try:
        current_mtime = DATA_FILE.stat().st_mtime
        if _last_data_mtime is None or current_mtime > _last_data_mtime:
            load_data_store()
    except Exception as e:
        print(f"⚠️ Не удалось синхронизировать данные: {e}")

# ══════════════════════════════════════════════════════
#  РАСЧЁТНЫЕ ФУНКЦИИ
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

def calc_total_from_units(form: str, unit_mg: float, units: float) -> float:
    """
    БАГ №3 ИСПРАВЛЕН: liquid = unit_mg * units (без деления).
    drops:  1 капля = 0.05 мл → total_drops = (unit_mg / 0.05) * units
    spray:  1 впрыск = 0.1 мл → total_sprays = (unit_mg / 0.1) * units
    liquid: unit_mg = мл в флаконе → total_ml = unit_mg * units
    прочие: unit_mg = мг в таблетке → total_mg = unit_mg * units
    """
    if form == "drops":
        return (unit_mg / 0.05) * units
    if form == "spray":
        return (unit_mg / 0.10) * units
    # liquid, tablets, capsules, sachet — простое умножение
    return unit_mg * units

def get_unit_label(form: str) -> str:
    return {
        "tablets": "табл.", "capsules": "капс.",
        "liquid":  "мл",    "drops":    "кап.",
        "spray":   "впрыск.", "sachet": "саше",
    }.get(form, "ед.")

def get_display_units(med: dict) -> tuple:
    form = med.get("form", "tablets")
    if form == "drops":  return "мл", "капель"
    if form == "spray":  return "мл", "впрыскиваний"
    if form == "liquid": return "мл", "мл"
    return "мг", "мг"

def doses_per_day(med: dict) -> int:
    """Количество приёмов в сутки = daily_mg / unit_mg."""
    form    = med.get("form", "tablets")
    daily   = med.get("daily_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return max(1, round(daily)) if daily > 0 else 1
    if unit_mg <= 0:
        return 1
    dpd = max(1, round(daily / unit_mg))
    # если в расписании явно больше приёмов — берём из расписания
    times = med.get("times", {})
    for t in times.values():
        if isinstance(t, list) and len(t) > dpd:
            dpd = len(t)
    return dpd

def calc_remaining_units(med: dict) -> float:
    form    = med.get("form", "tablets")
    total   = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return total
    return (total / unit_mg) if unit_mg > 0 else 0

def calc_daily_units(med: dict) -> float:
    form     = med.get("form", "tablets")
    daily_mg = med.get("daily_mg", 0) or 0
    unit_mg  = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return daily_mg
    return (daily_mg / unit_mg) if unit_mg > 0 else 0

def calc_days_left_in_course(med: dict) -> int:
    """Дней до конца КУРСА (по taken_doses)."""
    cd = med.get("course_days") or 0
    if cd > 0:
        taken  = med.get("taken_doses", 0) or 0
        dpd    = doses_per_day(med)
        passed = taken / dpd
        return max(0, int(cd - passed))
    else:
        dmg = med.get("daily_mg") or 0
        if dmg <= 0:
            return 0
        return int((med.get("total_mg") or 0) // dmg)

def calc_stock_days(med: dict) -> int:
    """Дней до исчерпания ЗАПАСА."""
    dmg = med.get("daily_mg") or 0
    if dmg <= 0:
        return 0
    return int((med.get("total_mg") or 0) // dmg)

def calc_progress(med: dict) -> int:
    """
    Прогресс шкалы (%).
    Курс: taken_doses / (course_days * doses_per_day) * 100 — персистентно.
    Пожизненно: % израсходованного от initial_units.
    """
    cd = med.get("course_days") or 0
    if cd > 0:
        taken       = med.get("taken_doses", 0) or 0
        dpd         = doses_per_day(med)
        total_doses = cd * dpd
        if total_doses <= 0:
            return 0
        return max(0, min(100, int(taken / total_doses * 100)))
    else:
        init    = med.get("initial_units")
        unit_mg = med.get("unit_mg") or 0
        total   = med.get("total_mg") or 0
        if init and init > 0 and unit_mg > 0:
            rem = total / unit_mg
            return max(0, min(100, int((1 - rem / init) * 100)))
        dmg = med.get("daily_mg") or 0
        if not dmg:
            return 0
        stock = calc_stock_days(med)
        taken = med.get("taken_doses", 0) or 0
        horizon = stock + taken
        if horizon <= 0:
            return 0
        return max(0, min(100, int(taken / horizon * 100)))

def is_course_finished(med: dict) -> bool:
    cd = med.get("course_days") or 0
    if cd <= 0 or not med.get("is_started"):
        return False
    total_doses = cd * doses_per_day(med)
    taken = med.get("taken_doses", 0) or 0
    return taken >= total_doses

def is_stock_empty(med: dict) -> bool:
    unit_mg = med.get("unit_mg") or 0
    total   = med.get("total_mg") or 0
    if unit_mg <= 0:
        return False
    return total < unit_mg

def is_snoozed_now(med: dict) -> bool:
    sval = med.get("snooze_until")
    if not sval:
        return False
    try:
        dt = TZ_MOSCOW.localize(datetime.strptime(sval, "%Y-%m-%d %H:%M"))
        return get_now() < dt
    except Exception:
        return False

def format_schedule(times: dict) -> str:
    if not isinstance(times, dict) or not times:
        return "не установлено"
    if times.get("Everyday"):
        return f"Каждый день: {', '.join(times['Everyday'])}"
    dn = {"0":"Пн","1":"Вт","2":"Ср","3":"Чт","4":"Пт","5":"Сб","6":"Вс"}
    lines = [f"{dn.get(d,d)}: {', '.join(times[d])}" for d in sorted(times) if times.get(d)]
    return "\n".join(lines) if lines else "не указано"

# ══════════════════════════════════════════════════════
#  МЕНЮ-КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════
DAYS_MAP = {
    "Everyday":"Каждый день", "Weekdays":"Будни (Пн-Пт)", "Weekends":"Выходные (Сб-Вс)",
    "0":"Понедельник","1":"Вторник","2":"Среда","3":"Четверг",
    "4":"Пятница","5":"Суббота","6":"Воскресенье",
}
FORM_LABELS = {
    "tablets":  ("таблетке","таблеток"),
    "capsules": ("капсуле","капсул"),
    "liquid":   ("флаконе","флаконов"),
    "drops":    ("флаконе","флаконов"),
    "spray":    ("флаконе","флаконов"),
    "sachet":   ("саше","саше"),
}

def main_menu():
    return ReplyKeyboardMarkup([
        ["➕ Добавить лекарство",    "▶️ Начать курс"],
        ["♻️ Докуплено / Пополнить", "🛠️ Изменить дозировку"],
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
    for key in ["Everyday","Weekdays","Weekends"]:
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

    # Регистрируем чат на диске, чтобы сайт при первой загрузке
    # мог сохранить лекарства под реальным Telegram chat_id.
    if cid not in data_store:
        data_store[cid] = {}
        await save_data_store_async()

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
    load_data_store()
    cid  = update.effective_chat.id
    text = update.message.text.strip()

    if text.lower() in {"старт","начать","start","/start","🚀 старт","меню старт"}:
        started_users.add(cid)
        user_states.pop(cid, None)
        await update.message.reply_text(f"Бот перезапущен (v{BOT_VERSION}).", reply_markup=main_menu())
        return

    if text == "➕ Добавить лекарство":
        user_states[cid] = {"flow":"add","step":"name","data":{}}
        await update.message.reply_text("Введите название лекарства:")
        return

    if text == "▶️ Начать курс":
        meds = data_store.get(cid, {})
        ns = [n for n,m in meds.items() if not m.get("is_started")]
        if not ns:
            await update.message.reply_text("Нет лекарств для запуска или все курсы уже начаты.")
            return
        kb = [[InlineKeyboardButton(n, callback_data=f"start_now:{n}")] for n in ns]
        await update.message.reply_text("Выберите курс для старта:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text in ("♻️ Докуплено / Пополнить","🔄 Докуплено / Пополнить"):
        load_data_store()
        meds = list(data_store.get(cid,{}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет"); return
        kb = [[InlineKeyboardButton(m, callback_data=f"refill:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text in ("🛠️ Изменить дозировку","🔧 Изменить дозировку"):
        load_data_store()
        meds = list(data_store.get(cid,{}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет"); return
        kb = [[InlineKeyboardButton(m, callback_data=f"dose:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text == "⏰ Напоминание (Дни/Время)":
        meds = list(data_store.get(cid,{}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет"); return
        kb = [[InlineKeyboardButton(m, callback_data=f"open_days:{m}")] for m in meds]
        await update.message.reply_text("Выберите лекарство:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text == "📋 Мои курсы и прогноз":
        await show_summary(update, context); return

    if text == "🗑 Удалить лекарство":
        meds = list(data_store.get(cid,{}).keys())
        if not meds:
            await update.message.reply_text("Лекарств нет"); return
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
        # Убеждаемся что d — это именно state["data"] (не копия)
        if "data" not in state:
            state["data"] = {}
        d = state["data"]

        if step == "name":
            d["name"] = text
            state["step"] = "form"
            await update.message.reply_text("Выберите форму:", reply_markup=form_menu())

        elif step == "unit_mg":
            d["unit_mg"] = float(text.replace(",","."))
            _, plural = FORM_LABELS.get(d["form"],("единице","единиц"))
            state["step"] = "units"
            await update.message.reply_text(f"Сколько {plural} купили?")

        elif step == "units":
            d["units"] = float(text.replace(",","."))
            state["step"] = "daily_mg"
            form = d.get("form","tablets")
            # БАГ №2 ИСПРАВЛЕН: tablets/capsules/sachet спрашиваем в мг
            q = {
                "drops":  "Сколько капель в сутки назначено?",
                "spray":  "Сколько впрыскиваний в сутки назначено?",
                "liquid": "Сколько мл в сутки назначено?",
            }.get(form, "Сколько мг. в сутки принимаете?")
            await update.message.reply_text(q)

        elif step == "daily_mg":
            d["daily_mg_input"] = float(text.replace(",","."))
            state["step"] = "course"
            await update.message.reply_text("Срок приёма:", reply_markup=course_menu())

        elif step == "course_value":
            # БАГ №4 ИСПРАВЛЕН: корректная обработка course_value
            try:
                val = int(float(text.replace(",",".")))
                if val <= 0:
                    await update.message.reply_text("⚠️ Введите число больше 0:")
                    return
                if d.get("course_type") == "months":
                    val *= 30
                d["course_days"] = val
                await _save_medicine(update.message, cid)
            except (ValueError, TypeError):
                await update.message.reply_text("⚠️ Введите целое число:")
        return

    if state["flow"] == "set_reminder":
        med_name = state["medicine"]
        day_key  = state["day_key"]
        try:
            med_data = data_store[cid][med_name]
            if text.lower() in ["0","нет","удалить"]:
                med_data["times"].pop(day_key, None)
                msg = f"🗑 Удалено для {DAYS_MAP.get(day_key,day_key)}"
            else:
                times = _parse_times(text)
                if not times:
                    await update.message.reply_text("⚠️ Неверный формат. Пример: 08:00, 20:00")
                    return
                med_data["times"][day_key] = times
                msg = f"✅ Сохранено для {DAYS_MAP.get(day_key,day_key)}"
            await save_data_store_async()
            user_states.pop(cid, None)
            # Формируем итоговое расписание для отображения
            all_times = []
            for dk, tv in med_data["times"].items():
                if tv:
                    day_label = DAYS_MAP.get(dk, dk)
                    all_times.append(f"{day_label}: {', '.join(tv)}")
            schedule_summary = "\n".join(all_times) if all_times else "не установлено"
            await update.message.reply_text(
                f"{msg}\n\n🔔 Текущее расписание:\n{schedule_summary}\n\nНастройте следующий день или вернитесь в меню:",
                reply_markup=days_menu(med_name, med_data["times"])
            )
        except Exception:
            await update.message.reply_text("✅ Изменения применены.")
            user_states.pop(cid, None)
        return

    if state["flow"] == "dose":
        # пользователь вводит в мг напрямую
        med = data_store[cid][state["medicine"]]
        try:
            new_daily_mg = float(text.replace(",","."))
            if new_daily_mg <= 0:
                await update.message.reply_text("⚠️ Введите число больше 0:")
                return
            med["daily_mg"] = new_daily_mg
            await save_data_store_async()
            days = calc_days_left_in_course(med)
            user_states.pop(cid, None)
            await update.message.reply_text(
                f"🔧 Дозировка изменена: {new_daily_mg} мг/сут. Хватит на {days} дн.",
                reply_markup=main_menu()
            )
        except (ValueError, TypeError):
            await update.message.reply_text("⚠️ Введите корректное число:")
        return

    if state["flow"] == "refill":
        if state["step"] == "units":
            try:
                units   = float(text.replace(",","."))
                med     = data_store[cid][state["medicine"]]
                form    = med.get("form","tablets")
                unit_mg = med.get("unit_mg",1)
                added   = calc_total_from_units(form, unit_mg, units)
                med["total_mg"] += added
                med["notified"]  = False
                if not med.get("course_days"):
                    med["initial_units"] = (med.get("initial_units") or 0) + units
                await save_data_store_async()
                days = calc_days_left_in_course(med)
                rem  = round(calc_remaining_units(med), 1)
                un   = get_unit_label(form)
                user_states.pop(cid, None)
                await update.message.reply_text(
                    f"🔄 Пополнено! Остаток: {rem} {un}. Хватит на {days} дн.",
                    reply_markup=main_menu()
                )
            except (ValueError, TypeError):
                await update.message.reply_text("⚠️ Введите корректное число:")
            return

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_data_store()
    q    = update.callback_query
    await q.answer()
    cid  = q.message.chat.id
    data = q.data

    if data == "main_menu":
        user_states.pop(cid, None)
        await q.message.reply_text("Главное меню:", reply_markup=main_menu())
        return

    if data.startswith("form_"):
        form = data.split("_",1)[1]
        if cid not in user_states or "data" not in user_states[cid]:
            user_states[cid] = {"flow":"add","step":"unit_mg","data":{}}
        user_states[cid]["data"]["form"] = form
        user_states[cid]["step"] = "unit_mg"
        prompts = {
            "drops":  "Объём флакона (мл)?",
            "spray":  "Сколько мл в одном флаконе?",
            "liquid": "Сколько мл. в одном флаконе?",
        }
        if form in prompts:
            await q.message.reply_text(prompts[form])
        else:
            singular, _ = FORM_LABELS.get(form,("единице","единиц"))
            await q.message.reply_text(f"Сколько мг в одной {singular}?")

        return

    if data.startswith("course_"):
        if cid not in user_states:
            return
        state = user_states[cid]
        d     = state.get("data", {})
        ctype = data.split("_",1)[1]
        d["course_type"] = ctype
        if ctype == "forever":
            d["course_days"] = None
            await _save_medicine(q, cid)
        else:
            # БАГ №4 ИСПРАВЛЕН: явно устанавливаем step и ждём ввода
            state["step"] = "course_value"
            unit_word = "месяцев" if ctype == "months" else "дней"
            await q.message.reply_text(f"Введите количество {unit_word}:")
        return

    if data.startswith("start_now:"):
        med_name = data.split(":",1)[1]
        if cid in data_store and med_name in data_store[cid]:
            med = data_store[cid][med_name]
            med["is_started"]  = True
            med["start_date"]  = get_now()
            med["taken_doses"] = 0
            med["notified"]    = False
            # БАГ №1: используем async сохранение
            await save_data_store_async()
            await q.message.reply_text(f"▶️ Курс «{med_name}» начат!", reply_markup=main_menu())
        return

    if data.startswith("open_days:"):
        med_name = data.split(":",1)[1]
        if med_name in data_store.get(cid,{}):
            med_data = data_store[cid][med_name]
            if not isinstance(med_data.get("times"),dict):
                med_data["times"] = {}
            await q.message.reply_text(
                f"📅 Настройка: «{med_name}». Выберите день:",
                reply_markup=days_menu(med_name, med_data["times"])
            )
        return

    if data.startswith("set_day:"):
        _, med_name, day_key = data.split(":",2)
        user_states[cid] = {"flow":"set_reminder","medicine":med_name,"day_key":day_key}
        await q.edit_message_text(
            f"⏰ Введите время для «{med_name}» ({DAYS_MAP.get(day_key,day_key)}).\n"
            "Формат: 8:00, 20:00 (через запятую). Отправьте «0» для удаления."
        )
        return

    if data.startswith("taken:"):
        med_name = data.split(":",1)[1]
        if cid in data_store and med_name in data_store[cid]:
            med = data_store[cid][med_name]
            # вычитаем одну таблетку (unit_mg)
            unit_mg = med.get("unit_mg") or med.get("daily_mg") or 1
            med["total_mg"]    = max(0, (med.get("total_mg") or 0) - unit_mg)
            med["taken_doses"] = (med.get("taken_doses") or 0) + 1
            # БАГ №1: async сохранение чтобы сайт сразу видел изменение
            await save_data_store_async()
            rem  = round(calc_remaining_units(med), 1)
            un   = get_unit_label(med.get("form","tablets"))
            prog = calc_progress(med)
            if is_course_finished(med):
                await q.edit_message_text(f"🎉 Курс «{med_name}» завершён! Молодец!")
            elif is_stock_empty(med) and med.get("is_started"):
                await q.edit_message_text(
                    f"⚠️ «{med_name}» закончились!\n"
                    f"Остаток: {rem} {un}. Пополните запас чтобы продолжить курс."
                )
            else:
                await q.edit_message_text(
                    f"✅ Приём «{med_name}» отмечен. Молодец!\n"
                    f"Остаток: {rem} {un} · Прогресс: {prog}%"
                )
        return

    if data.startswith("later:"):
        # БАГ №5 ИСПРАВЛЕН: fallback через asyncio.create_task
        parts    = data.split(":",2)
        minutes  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        med_name = parts[2] if len(parts) > 2 else ""

        if context.job_queue:
            try:
                context.job_queue.run_once(
                    _send_delayed_reminder,
                    when=minutes * 60,
                    data={"chat_id":cid,"med_name":med_name}
                )
            except Exception as e:
                print(f"job_queue failed: {e}, using asyncio fallback")
                asyncio.create_task(
                    _send_delayed_reminder_fallback(cid, med_name, minutes)
                )
        else:
            asyncio.create_task(
                _send_delayed_reminder_fallback(cid, med_name, minutes)
            )

        await q.edit_message_text(f"⏳ Напомню про «{med_name}» через {minutes} мин.")
        if cid in data_store and med_name in data_store[cid]:
            data_store[cid][med_name]["snooze_until"] = (get_now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
            await save_data_store_async()
        return

    if ":" in data:
        action, med_name = data.split(":",1)
        if action == "dose":
            user_states[cid] = {"flow":"dose","medicine":med_name,"step":"daily_mg","data":{}}
            await q.message.reply_text("Введите новую суточную дозировку (мг/сут):")
        elif action == "refill":
            med = data_store.get(cid,{}).get(med_name,{})
            un  = get_unit_label(med.get("form","tablets"))
            user_states[cid] = {"flow":"refill","medicine":med_name,"step":"units","data":{}}
            await q.message.reply_text(f"Сколько {un} докупили?")
        elif action == "delete":
            data_store.get(cid,{}).pop(med_name, None)
            await save_data_store_async()
            await q.edit_message_text("🗑 Лекарство удалено")

# ══════════════════════════════════════════════════════
#  СОХРАНЕНИЕ НОВОГО ЛЕКАРСТВА
# ══════════════════════════════════════════════════════
async def _save_medicine(src, cid: int):
    d        = user_states[cid]["data"]
    form     = d["form"]
    unit_mg  = d["unit_mg"]
    units    = d["units"]
    # daily_mg_input: для drops/spray — единицы, для остальных — мг
    daily_input = d.get("daily_mg_input") or 1
    course   = d.get("course_days")  # None = пожизненно

    # БАГ №3 ИСПРАВЛЕН: calc_total_from_units правильно обрабатывает liquid
    total    = calc_total_from_units(form, unit_mg, units)

    # daily_mg в внутренних единицах
    # drops/spray: пользователь вводил в каплях/впрыскиваниях — сохраняем как есть
    # остальные: пользователь вводил в мг — сохраняем как есть
    daily_mg = daily_input

    data_store.setdefault(cid, {})[d["name"]] = {
        "form":              form,
        "daily_mg":          daily_mg,
        "unit_mg":           unit_mg,
        "total_mg":          total,
        "initial_units":     units,
        "course_days":       course,
        "taken_doses":       0,
        "created":           get_now(),
        "is_started":        False,
        "start_date":        None,
        "times":             {},
        "notified":          False,
        "last_reminder_key": None,
        "last_9am_key":      None,
    }

    await save_data_store_async()
    print(f"✅ Сохранено лекарство '{d['name']}' для chat_id={cid}, файл={DATA_FILE}")
    med  = data_store[cid][d["name"]]
    days = calc_days_left_in_course(med)
    un   = get_unit_label(form)
    rem  = round(calc_remaining_units(med), 1)
    # Для drops/spray суточная доза в единицах, для остальных в мг
    dose_display = f"{daily_mg} {'мл/сут' if form == 'liquid' else 'мг/сут' if form not in ('drops','spray') else (get_unit_label(form) + '/сут')}"

    msg = (
        f"✅ Лекарство *{d['name']}* успешно добавлено!\n\n"
        f"Расход: {dose_display}\n"
        f"Запас: {rem} {un}\n"
        f"Хватит на: {days} дней\n\n"
        f"⚠️ Нажмите «▶️ Начать курс» для старта отсчёта."
    )

    # Определяем объект для отправки сообщения
    # src может быть Message или CallbackQuery
    try:
        if hasattr(src, "reply_text"):
            # src — это Message — используем напрямую
            await src.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
        elif hasattr(src, "message") and hasattr(src.message, "reply_text"):
            # src — это CallbackQuery — используем src.message
            try:
                # Сначала попробуем отредактировать исходное сообщение
                await src.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass  # Если не получилось — не страшно
            await src.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
        else:
            print(f"_save_medicine: неизвестный тип src: {type(src)}")
    except Exception as e:
        print(f"_save_medicine reply error: {e}")
        # Аварийный вариант — через bot напрямую
        if bot_application:
            try:
                await bot_application.bot.send_message(
                    chat_id=cid, text=msg,
                    parse_mode="Markdown",
                    reply_markup=main_menu()
                )
            except Exception as e2:
                print(f"_save_medicine bot.send_message error: {e2}")

    user_states.pop(cid, None)

# ══════════════════════════════════════════════════════
#  СВОДКА И ПРОГНОЗ
# ══════════════════════════════════════════════════════
async def show_summary(update_or_obj, context=None):
    load_data_store()
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
        dl     = calc_days_left_in_course(med)
        sd     = calc_stock_days(med)
        cd     = med.get("course_days") or 0
        status = "▶️ Идёт приём" if med.get("is_started") else "⏸ Ожидание старта"
        sched  = format_schedule(med.get("times",{}))
        un     = get_unit_label(med.get("form","tablets"))
        rem    = round(calc_remaining_units(med), 1)
        dly    = round(calc_daily_units(med), 2)
        prog   = calc_progress(med)

        lines.append(f"💊 *{name}* ({status})")
        lines.append(f"Расписание: {sched}")
        lines.append(f"Расход: {med.get('daily_mg',0)} мг/сут ({dly} {un}/сут)")
        lines.append(f"Остаток: {rem} {un}")
        lines.append(f"Прогресс: {prog}%")
        if cd > 0:
            lines.append(f"До конца курса: {dl} дн.")
            lines.append(f"Запас на: {sd} дн.")
            if med.get("is_started") and med.get("start_date"):
                start = _parse_dt(med["start_date"])
                if start:
                    end_dt  = start + timedelta(days=cd)
                    enough  = sd >= dl
                    lines.append(f"Конец курса: {end_dt.strftime('%d.%m.%Y')}")
                    lines.append(f"Запас {'✅ хватит' if enough else '❌ НЕ хватит'} до конца курса")
        else:
            lines.append(f"Хватит на: {dl} дн.")
            lines.append("Режим: пожизненный ♾")

        lines.append("⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯")

    await msg_obj.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())

# ══════════════════════════════════════════════════════
#  ЦИКЛ НАПОМИНАНИЙ
# ══════════════════════════════════════════════════════
async def _reminder_loop():
    while True:
        try:
            load_data_store()
            now   = get_now()
            ts    = now.strftime("%H:%M")
            wd    = str(now.weekday())
            is_we = now.weekday() >= 5

            for cid, meds in list(data_store.items()):
                for name, m in list(meds.items()):
                    if not m.get("is_started"):
                        continue
                    if is_snoozed_now(m):
                        continue
                    t = m.get("times", {})
                    check = []
                    if t.get("Everyday"):               check += t["Everyday"]
                    if is_we and t.get("Weekends"):     check += t["Weekends"]
                    if not is_we and t.get("Weekdays"): check += t["Weekdays"]
                    if t.get(wd):                       check += t[wd]

                    if ts not in set(check):
                        continue
                    rkey = f"{now.date().isoformat()}:{ts}:{name}"
                    if m.get("last_reminder_key") == rkey:
                        continue

                    dly_u = round(calc_daily_units(m) / max(1,len(set(check))), 2)
                    un    = get_unit_label(m.get("form","tablets"))
                    if bot_application:
                        await bot_application.bot.send_message(
                            cid,
                            f"⏰ Время принимать: *{name}*\nДоза: {dly_u} {un}",
                            parse_mode="Markdown",
                            reply_markup=reminder_action_menu(name)
                        )
                    m["last_reminder_key"] = rkey
                    # БАГ №1: async сохранение
                    await save_data_store_async()

                    # предупреждение о заканчивающемся запасе
                    sl = calc_stock_days(m)
                    if sl <= 7 and not m.get("notified") and bot_application:
                        await bot_application.bot.send_message(
                            cid,
                            f"⚠️ *{name}*: запаса осталось на {sl} дн.! Пора докупить.",
                            parse_mode="Markdown"
                        )
                        m["notified"] = True
                        await save_data_store_async()

        except Exception as e:
            print(f"Ошибка reminder_loop: {e}")
        await asyncio.sleep(30)

async def _send_delayed_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отложенное напоминание через job_queue."""
    job      = context.job
    med_name = job.data["med_name"]
    cid      = job.data["chat_id"]
    meds     = data_store.get(cid, {})
    if med_name in meds and bot_application:
        m   = meds[med_name]
        dly = round(calc_daily_units(m), 2)
        un  = get_unit_label(m.get("form","tablets"))
        await bot_application.bot.send_message(
            cid,
            f"🔔 Напоминание: *{med_name}*\nДоза: {dly} {un}",
            parse_mode="Markdown",
            reply_markup=reminder_action_menu(med_name)
        )

async def _send_delayed_reminder_fallback(cid: int, med_name: str, minutes: int):
    """БАГ №5 ИСПРАВЛЕН: fallback через asyncio.create_task когда job_queue недоступен."""
    await asyncio.sleep(minutes * 60)
    meds = data_store.get(cid, {})
    if med_name in meds and bot_application:
        m   = meds[med_name]
        dly = round(calc_daily_units(m), 2)
        un  = get_unit_label(m.get("form","tablets"))
        try:
            await bot_application.bot.send_message(
                cid,
                f"🔔 Напоминание: *{med_name}*\nДоза: {dly} {un}",
                parse_mode="Markdown",
                reply_markup=reminder_action_menu(med_name)
            )
        except Exception as e:
            print(f"Ошибка отложенного напоминания: {e}")

def _parse_times(text: str) -> list:
    text  = text.replace("24:00","00:00")
    clean = text.replace(","," ").replace(";"," ").replace("."," ").replace("\n"," ")
    found = re.findall(r'\b([0-9]{1,2})[: ]([0-9]{2})\b', clean)
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
    await application.bot.set_my_commands([BotCommand("start","перезапустить бота")])
    load_data_store()
    asyncio.create_task(_reminder_loop())
    print(f"✅ Бот v{BOT_VERSION} инициализирован. DATA_FILE={DATA_FILE}")

def main():
    app_bot = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(buttons))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print(f"🤖 Telegram бот v{BOT_VERSION} запускается...")
    app_bot.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
