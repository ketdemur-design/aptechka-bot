import os
import json
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import uvicorn
import pytz
import logging
from pywebpush import webpush, WebPushException

from settings import APP_VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════
TZ_MOSCOW  = pytz.timezone('Europe/Moscow')
DATA_FILE  = Path(os.getenv("DATA_FILE", "meds_data.json"))
STATIC_DIR = Path(__file__).parent / "static"

logger.info(f"📁 server.py  данные: {DATA_FILE.absolute()}")

app = FastAPI(title="MedTracker", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers.update(NO_CACHE_HEADERS)
    return response
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ══════════════════════════════════════════════════════
#  МОДЕЛИ
# ══════════════════════════════════════════════════════
class AddMedicineRequest(BaseModel):
    chat_id:     Optional[int] = 0
    name:        str
    form:        str
    unit_mg:     float
    units:       float
    daily_mg:    float
    course_days: int = 0

class UpdateMedicineRequest(BaseModel):
    chat_id:   int
    med_name:  str
    daily_mg:  Optional[float] = None
    add_stock: Optional[float] = None

class StartCourseRequest(BaseModel):
    chat_id:  int
    med_name: str

class TakenRequest(BaseModel):
    chat_id:  int
    med_name: str

class ScheduleRequest(BaseModel):
    chat_id: int
    med_name: str
    day_key: str
    times: list[str]

class SnoozeRequest(BaseModel):
    chat_id: int
    med_name: str
    minutes: int

class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: dict


# ══════════════════════════════════════════════════════
#  РАБОТА С ДАННЫМИ
# ══════════════════════════════════════════════════════
def get_now() -> datetime:
    return datetime.now(TZ_MOSCOW)

def _load() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return {int(k): v for k, v in parsed.items() if str(k).isdigit()} | {k: v for k, v in parsed.items() if not str(k).isdigit()}
    except Exception as e:
        logger.error(f"load error: {e}")
        return {}

def _save(store: dict) -> bool:
    """БАГ №1 ИСПРАВЛЕН: запись в тот же DATA_FILE через tmp → replace."""
    try:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({str(k): v for k, v in store.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        tmp.replace(DATA_FILE)
        return True
    except Exception as e:
        logger.error(f"save error: {e}")
        return False

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

# ══════════════════════════════════════════════════════
#  РАСЧЁТНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════
def calc_total_from_units(form: str, unit_mg: float, units: float) -> float:
    """
    БАГ №3 ИСПРАВЛЕН: liquid = unit_mg * units (простое умножение).
    drops:  1 капля = 0.05 мл → (мл_флакона / 0.05) * кол-во_флаконов
    spray:  1 впрыск = 0.1 мл → (мл_флакона / 0.10) * кол-во_флаконов
    liquid: мл_бутылки * кол-во_бутылок
    прочие: мг_таблетки * кол-во_таблеток
    """
    if form == "drops":
        return (unit_mg / 0.05) * units
    if form == "spray":
        return (unit_mg / 0.10) * units
    return unit_mg * units  # liquid, tablets, capsules, sachet

def get_unit_name(form: str) -> str:
    return {
        "tablets":  "табл.", "capsules": "капс.",
        "liquid":   "мл",    "drops":    "кап.",
        "spray":    "впрыск.", "sachet": "саше",
    }.get(form, "ед.")

def calc_remaining_units(med: dict) -> float:
    """Остаток в штуках: total_mg / unit_mg."""
    form    = med.get("form", "tablets")
    total   = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return total
    return (total / unit_mg) if unit_mg > 0 else 0

def calc_daily_units(med: dict) -> float:
    """Суточный расход в штуках: daily_mg / unit_mg."""
    form     = med.get("form", "tablets")
    daily_mg = med.get("daily_mg", 0) or 0
    unit_mg  = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return daily_mg
    return (daily_mg / unit_mg) if unit_mg > 0 else 0

def doses_per_day(med: dict) -> int:
    """
    Количество приёмов в сутки = daily_mg / unit_mg.
    Пример: 200 мг/сут при таблетке 100 мг → 2 приёма.
    """
    form    = med.get("form", "tablets")
    daily   = med.get("daily_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return max(1, round(daily)) if daily > 0 else 1
    if unit_mg <= 0:
        return 1
    dpd = max(1, round(daily / unit_mg))
    times = med.get("times", {})
    for t in times.values():
        if isinstance(t, list) and len(t) > dpd:
            dpd = len(t)
    return dpd

def calc_days_left_in_course(med: dict) -> int:
    """
    Дней до конца КУРСА по taken_doses.
    Пожизненно: дней до исчерпания запаса.
    """
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
    """Дней до исчерпания ЗАПАСА = total_mg / daily_mg."""
    dmg = med.get("daily_mg") or 0
    if dmg <= 0:
        return 0
    return int((med.get("total_mg") or 0) // dmg)

def calc_progress(med: dict) -> int:
    """
    Прогресс (%):
    Курс:       taken_doses / (course_days * doses_per_day) * 100
    Пожизненно: % израсходованного от initial_units
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
        stock  = calc_stock_days(med)
        taken  = med.get("taken_doses", 0) or 0
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

def format_schedule(times: dict) -> str:
    if not isinstance(times, dict) or not times:
        return "не указано"
    if times.get("Everyday"):
        return f"Каждый день: {', '.join(times['Everyday'])}"
    dn    = {"0":"Пн","1":"Вт","2":"Ср","3":"Чт","4":"Пт","5":"Сб","6":"Вс"}
    parts = [f"{dn.get(d,d)}: {', '.join(times[d])}" for d in sorted(times) if times.get(d)]
    return " | ".join(parts) if parts else "не указано"

def build_med_row(name: str, med: dict, chat_id: int) -> dict:
    form             = med.get("form", "tablets")
    cd               = med.get("course_days") or 0
    is_started       = med.get("is_started", False)
    days_left        = calc_days_left_in_course(med)
    stock_days       = calc_stock_days(med)
    progress         = calc_progress(med)
    rem_units        = calc_remaining_units(med)
    daily_units      = calc_daily_units(med)
    daily_mg         = med.get("daily_mg", 0)
    unit_mg          = med.get("unit_mg", 0)
    unit_name        = get_unit_name(form)
    schedule         = format_schedule(med.get("times", {}))
    course_done      = is_course_finished(med)
    stock_empty      = is_stock_empty(med)
    need_refill      = stock_empty and is_started and not course_done

    end_date         = None
    course_days_left = None
    is_enough        = True

    if cd > 0 and is_started:
        course_days_left = days_left
        is_enough        = stock_days >= course_days_left
        start = _parse_dt(med.get("start_date"))
        if start:
            end_date = (start + timedelta(days=cd)).strftime("%d.%m.%Y")

    return {
        "name":             name,
        "chat_id":          chat_id,
        "form":             form,
        "daily_mg":         daily_mg,
        "unit_mg":          unit_mg,
        "total_mg":         med.get("total_mg", 0),
        "remaining_units":  round(rem_units, 2),
        "daily_units":      round(daily_units, 2),
        "unit_name":        unit_name,
        "dose_line":        f"{daily_mg} мг/сут",
        "course_days":      cd,
        "is_started":       is_started,
        "progress":         progress,
        "taken_doses":      med.get("taken_doses", 0) or 0,
        "days_left":        days_left,
        "stock_days":       stock_days,
        "is_enough":        is_enough,
        "end_date":         end_date,
        "course_days_left": course_days_left,
        "course_done":      course_done,
        "need_refill":      need_refill,
        "stock_empty":      stock_empty,
        "schedule":         schedule,
        "times":            med.get("times", {}),
    }

# ══════════════════════════════════════════════════════
#  API ЭНДПОИНТЫ
# ══════════════════════════════════════════════════════
@app.get("/api/meds")
async def get_meds(chat_id: Optional[int] = None):
    logger.info(f"GET /api/meds  chat_id={chat_id}  file={DATA_FILE}")
    store = _load()
    logger.info(f"  store keys: {list(store.keys())}")

    if chat_id is not None:
        # конкретный chat_id запрошен
        result = [build_med_row(n, m, chat_id)
                  for n, m in store.get(chat_id, {}).items()]
    else:
        # Возвращаем ВСЕ лекарства из всех chat_id
        # (бот и сайт могут использовать разные chat_id)
        result = []
        for cid, meds in store.items():
            for n, m in meds.items():
                result.append(build_med_row(n, m, cid))

    logger.info(f"  → {len(result)} записей")
    return result


@app.post("/api/meds/add")
async def add_medicine(req: AddMedicineRequest):
    logger.info(f"POST /api/meds/add  name={req.name}")
    try:
        store    = _load()
        chat_id  = req.chat_id or next(iter(store), 12345)
        req_name = req.name.strip()
        if not req_name:
            raise HTTPException(400, "Название не может быть пустым")
        if req_name in store.get(chat_id, {}):
            raise HTTPException(400, "Лекарство с таким названием уже существует")

        # БАГ №3 ИСПРАВЛЕН: calc_total_from_units правильно считает liquid
        total = calc_total_from_units(req.form, req.unit_mg, req.units)

        store.setdefault(chat_id, {})[req_name] = {
            "form":              req.form,
            "daily_mg":          req.daily_mg,
            "unit_mg":           req.unit_mg,
            "total_mg":          total,
            "initial_units":     req.units,
            "course_days":       req.course_days if req.course_days > 0 else None,
            "taken_doses":       0,
            "created":           get_now().isoformat(),
            "is_started":        False,
            "start_date":        None,
            "times":             {},
            "notified":          False,
            "last_reminder_key": None,
            "last_9am_key":      None,
        }

        if not _save(store):
            raise HTTPException(500, "Ошибка сохранения")

        med  = store[chat_id][req_name]
        un   = get_unit_name(req.form)
        rem  = round(calc_remaining_units(med), 1)
        dly  = round(calc_daily_units(med), 1)
        days = calc_days_left_in_course(med)

        return {
            "success": True,
            "message": (f"✅ Лекарство успешно добавлено!\n"
                        f"Запас: {rem} {un}. Расход: {req.daily_mg} мг/сут. "
                        f"Хватит на {days} дней."),
            "name": req_name
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/meds/update")
async def update_medicine(req: UpdateMedicineRequest):
    logger.info(f"POST /api/meds/update  {req.med_name}")
    try:
        store = _load()
        if req.chat_id not in store:
            raise HTTPException(404, "Чат не найден")
        if req.med_name not in store[req.chat_id]:
            raise HTTPException(404, "Лекарство не найдено")

        med = store[req.chat_id][req.med_name]
        un  = get_unit_name(med.get("form","tablets"))

        if req.daily_mg is not None:
            med["daily_mg"] = req.daily_mg
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left_in_course(med)
            return {"success": True, "message": f"✅ Дозировка: {req.daily_mg} мг/сут. Хватит на {days} дн."}

        if req.add_stock is not None:
            unit_mg = med.get("unit_mg", 1)
            added   = calc_total_from_units(med.get("form","tablets"), unit_mg, req.add_stock)
            med["total_mg"] += added
            med["notified"]  = False
            if not med.get("course_days"):
                med["initial_units"] = (med.get("initial_units") or 0) + req.add_stock
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left_in_course(med)
            rem  = round(calc_remaining_units(med), 1)
            return {"success": True, "message": f"🔄 Пополнено! Остаток: {rem} {un}. Хватит на {days} дн."}

        raise HTTPException(400, "Нечего обновлять")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/meds/start")
async def start_course(req: StartCourseRequest):
    logger.info(f"POST /api/meds/start  {req.med_name}")
    try:
        store = _load()
        if req.chat_id not in store:
            raise HTTPException(404, "Чат не найден")
        if req.med_name not in store[req.chat_id]:
            raise HTTPException(404, "Лекарство не найдено")
        med = store[req.chat_id][req.med_name]
        med["is_started"]  = True
        med["start_date"]  = get_now().isoformat()
        med["taken_doses"] = 0
        med["notified"]    = False
        if not _save(store):
            raise HTTPException(500, "Ошибка сохранения")
        return {"success": True, "message": f"▶️ Курс «{req.med_name}» начат!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/taken")
async def mark_taken(req: TakenRequest):
    """
    БАГ №1 ИСПРАВЛЕН: данные записываются в тот же DATA_FILE.
    Вычитает одну таблетку (unit_mg), инкрементирует taken_doses.
    Возвращает updated-поля для мгновенного обновления шкалы.
    """
    logger.info(f"POST /api/taken  {req.med_name}")
    try:
        store       = _load()
        updated     = {}
        course_done = False
        need_refill = False

        if req.chat_id in store and req.med_name in store[req.chat_id]:
            med = store[req.chat_id][req.med_name]

            # вычитаем одну таблетку (unit_mg), а не суточную дозу
            unit_mg = med.get("unit_mg") or med.get("daily_mg") or 1
            med["total_mg"]    = max(0, (med.get("total_mg") or 0) - unit_mg)
            med["taken_doses"] = (med.get("taken_doses") or 0) + 1

            # БАГ №1: _save записывает в тот же DATA_FILE что и бот
            _save(store)

            course_done = is_course_finished(med)
            need_refill = is_stock_empty(med) and med.get("is_started") and not course_done

            updated = {
                "total_mg":        round(med["total_mg"], 4),
                "remaining_units": round(calc_remaining_units(med), 2),
                "daily_units":     round(calc_daily_units(med), 2),
                "days_left":       calc_days_left_in_course(med),
                "stock_days":      calc_stock_days(med),
                "progress":        calc_progress(med),
                "taken_doses":     med["taken_doses"],
                "course_done":     course_done,
                "need_refill":     need_refill,
                "stock_empty":     is_stock_empty(med),
            }

        if course_done:
            msg = "🎉 Курс завершён!"
        elif need_refill:
            msg = "⚠️ Таблетки закончились! Пополните запас."
        else:
            msg = "✅ Приём отмечен!"

        return {"success": True, "message": msg, "updated": updated}

    except Exception as e:
        logger.error(e)
        return {"success": True, "message": "✅ Приём отмечен!", "updated": {}}

@app.post("/api/meds/schedule")
async def set_schedule(req: ScheduleRequest):
    store = _load()
    if req.chat_id not in store or req.med_name not in store[req.chat_id]:
        raise HTTPException(404, "Лекарство не найдено")
    med = store[req.chat_id][req.med_name]
    times = [t.strip() for t in req.times if isinstance(t, str) and re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", t.strip())]
    if not times:
        raise HTTPException(400, "Нужно передать хотя бы одно корректное время в формате HH:MM")
    if not isinstance(med.get("times"), dict):
        med["times"] = {}
    med["times"][req.day_key] = sorted(set(times))
    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения")
    return {"success": True, "message": "Расписание сохранено", "times": med["times"]}

@app.post("/api/meds/snooze")
async def snooze_schedule(req: SnoozeRequest):
    store = _load()
    if req.chat_id not in store or req.med_name not in store[req.chat_id]:
        raise HTTPException(404, "Лекарство не найдено")
    if req.minutes <= 0:
        raise HTTPException(400, "minutes должен быть больше 0")
    med = store[req.chat_id][req.med_name]
    med["snooze_until"] = (get_now() + timedelta(minutes=req.minutes)).strftime("%Y-%m-%d %H:%M")
    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения")
    return {"success": True, "message": f"Напоминание отложено на {req.minutes} мин"}


@app.delete("/api/meds")
async def delete_medicine(chat_id: int, med_name: str):
    logger.info(f"DELETE /api/meds  {med_name}")
    try:
        store = _load()
        if chat_id in store and med_name in store[chat_id]:
            del store[chat_id][med_name]
            if _save(store):
                return {"success": True, "message": "🗑 Лекарство удалено"}
        raise HTTPException(404, "Лекарство не найдено")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.get("/api/version")
async def get_version():
    return {"version": APP_VERSION}

@app.get("/health")
async def health():
    return {"status":"ok","version":APP_VERSION,"data_file":str(DATA_FILE),"exists":DATA_FILE.exists()}

@app.get("/test")
async def test():
    return {"message":"Server is working!","version":APP_VERSION}

@app.get("/")
async def root():
    p = STATIC_DIR / "index.html"
    return FileResponse(p, media_type='text/html', headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0'}) if p.exists() else {"status":"ok","error":"index.html not found"}

@app.get("/service-worker.js")
async def service_worker():
    p = STATIC_DIR / "service-worker.js"
    return FileResponse(p, media_type='application/javascript', headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0','Service-Worker-Allowed':'/'}) if p.exists() else {"status":"ok","error":"service-worker.js not found"}


VAPID_PRIVATE_KEY = "sHaxRrXHj95RPhQh0hXgRasfwgIYaGuybHJAVzdzAgk"
VAPID_PUBLIC_KEY = "BBqYJditjsv4ZXeaQvjX4irpgLjYdBxovGtAfjKMEfAmlZRy5LdQVPk6i755jyCUjvrB2r0oEX-Mhxx8Mes7NFI"
VAPID_CLAIMS = {"sub": "mailto:admin@example.com"}

def _send_web_push(subscription: dict, title: str, body: str) -> bool:
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS,
        )
        return True
    except WebPushException as e:
        logger.warning(f"push send failed: {e}")
        return False

def send_push_for_due_medicines() -> int:
    now = get_now()
    now_hhmm = now.strftime("%H:%M")
    weekday = str(now.weekday())
    day_key = now.strftime("%Y-%m-%d")
    sent = 0

    store = _load()
    subscriptions = store.get("_push_subscriptions", []) if isinstance(store, dict) else []
    if not subscriptions:
        return 0

    for chat_id, meds in store.items():
        if isinstance(chat_id, str):
            continue
        for med_name, med in meds.items():
            if not med.get("is_started"):
                continue

            snooze_until = _parse_dt(med.get("snooze_until"))
            if snooze_until and snooze_until > now:
                continue

            times = med.get("times", {})
            today_times = []
            if isinstance(times.get("Everyday"), list):
                today_times.extend(times.get("Everyday", []))
            if now.weekday() >= 5 and isinstance(times.get("Weekends"), list):
                today_times.extend(times.get("Weekends", []))
            if now.weekday() < 5 and isinstance(times.get("Weekdays"), list):
                today_times.extend(times.get("Weekdays", []))
            if isinstance(times.get(weekday), list):
                today_times.extend(times.get(weekday, []))

            if now_hhmm not in set(today_times):
                continue

            reminder_key = f"{day_key}:{now_hhmm}"
            if med.get("last_push_key") == reminder_key:
                continue

            title = "💊 Напоминание о приёме"
            body = f"Пора принять: {med_name}"
            for sub in subscriptions:
                if _send_web_push(sub, title, body):
                    sent += 1

            med["last_push_key"] = reminder_key

    _save(store)
    return sent

@app.post("/api/subscribe")
async def subscribe_push(subscription: PushSubscriptionRequest):
    store = _load()
    subs = store.get("_push_subscriptions", [])
    if not any(item.get("endpoint") == subscription.endpoint for item in subs):
        subs.append(subscription.model_dump())
    store["_push_subscriptions"] = subs
    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения push-подписки")
    return {"success": True, "message": "Push-подписка сохранена", "public_key": VAPID_PUBLIC_KEY}

@app.post("/api/push/check")
async def trigger_push_check():
    sent = send_push_for_due_medicines()
    return {"success": True, "sent": sent}


# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════
def run_server():
    port = int(os.getenv("PORT", 3000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"\n{'='*55}")
    print(f"🚀  MedTracker API  v{APP_VERSION}")
    print(f"📁  данные:  {DATA_FILE.absolute()}")
    print(f"📄  HTML:    {STATIC_DIR / 'index.html'}")
    print(f"🌐  http://{host}:{port}")
    print(f"{'='*55}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    run_server()
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import uvicorn
import pytz
import logging
from pywebpush import webpush, WebPushException

from settings import APP_VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════
TZ_MOSCOW  = pytz.timezone('Europe/Moscow')
DATA_FILE  = Path(os.getenv("DATA_FILE", "meds_data.json"))
STATIC_DIR = Path(__file__).parent / "static"

logger.info(f"📁 server.py  данные: {DATA_FILE.absolute()}")

app = FastAPI(title="MedTracker", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers.update(NO_CACHE_HEADERS)
    return response
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ══════════════════════════════════════════════════════
#  МОДЕЛИ
# ══════════════════════════════════════════════════════
class AddMedicineRequest(BaseModel):
    chat_id:     Optional[int] = 0
    name:        str
    form:        str
    unit_mg:     float
    units:       float
    daily_mg:    float
    course_days: int = 0

class UpdateMedicineRequest(BaseModel):
    chat_id:   int
    med_name:  str
    daily_mg:  Optional[float] = None
    add_stock: Optional[float] = None

class StartCourseRequest(BaseModel):
    chat_id:  int
    med_name: str

class TakenRequest(BaseModel):
    chat_id:  int
    med_name: str

class ScheduleRequest(BaseModel):
    chat_id: int
    med_name: str
    day_key: str
    times: list[str]

class SnoozeRequest(BaseModel):
    chat_id: int
    med_name: str
    minutes: int

class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: dict


# ══════════════════════════════════════════════════════
#  РАБОТА С ДАННЫМИ
# ══════════════════════════════════════════════════════
def get_now() -> datetime:
    return datetime.now(TZ_MOSCOW)

def _load() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return {int(k): v for k, v in parsed.items() if str(k).isdigit()} | {k: v for k, v in parsed.items() if not str(k).isdigit()}
    except Exception as e:
        logger.error(f"load error: {e}")
        return {}

def _save(store: dict) -> bool:
    """БАГ №1 ИСПРАВЛЕН: запись в тот же DATA_FILE через tmp → replace."""
    try:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({str(k): v for k, v in store.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        tmp.replace(DATA_FILE)
        return True
    except Exception as e:
        logger.error(f"save error: {e}")
        return False

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

# ══════════════════════════════════════════════════════
#  РАСЧЁТНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════
def calc_total_from_units(form: str, unit_mg: float, units: float) -> float:
    """
    БАГ №3 ИСПРАВЛЕН: liquid = unit_mg * units (простое умножение).
    drops:  1 капля = 0.05 мл → (мл_флакона / 0.05) * кол-во_флаконов
    spray:  1 впрыск = 0.1 мл → (мл_флакона / 0.10) * кол-во_флаконов
    liquid: мл_бутылки * кол-во_бутылок
    прочие: мг_таблетки * кол-во_таблеток
    """
    if form == "drops":
        return (unit_mg / 0.05) * units
    if form == "spray":
        return (unit_mg / 0.10) * units
    return unit_mg * units  # liquid, tablets, capsules, sachet

def get_unit_name(form: str) -> str:
    return {
        "tablets":  "табл.", "capsules": "капс.",
        "liquid":   "мл",    "drops":    "кап.",
        "spray":    "впрыск.", "sachet": "саше",
    }.get(form, "ед.")

def calc_remaining_units(med: dict) -> float:
    """Остаток в штуках: total_mg / unit_mg."""
    form    = med.get("form", "tablets")
    total   = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return total
    return (total / unit_mg) if unit_mg > 0 else 0

def calc_daily_units(med: dict) -> float:
    """Суточный расход в штуках: daily_mg / unit_mg."""
    form     = med.get("form", "tablets")
    daily_mg = med.get("daily_mg", 0) or 0
    unit_mg  = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return daily_mg
    return (daily_mg / unit_mg) if unit_mg > 0 else 0

def doses_per_day(med: dict) -> int:
    """
    Количество приёмов в сутки = daily_mg / unit_mg.
    Пример: 200 мг/сут при таблетке 100 мг → 2 приёма.
    """
    form    = med.get("form", "tablets")
    daily   = med.get("daily_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return max(1, round(daily)) if daily > 0 else 1
    if unit_mg <= 0:
        return 1
    dpd = max(1, round(daily / unit_mg))
    times = med.get("times", {})
    for t in times.values():
        if isinstance(t, list) and len(t) > dpd:
            dpd = len(t)
    return dpd

def calc_days_left_in_course(med: dict) -> int:
    """
    Дней до конца КУРСА по taken_doses.
    Пожизненно: дней до исчерпания запаса.
    """
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
    """Дней до исчерпания ЗАПАСА = total_mg / daily_mg."""
    dmg = med.get("daily_mg") or 0
    if dmg <= 0:
        return 0
    return int((med.get("total_mg") or 0) // dmg)

def calc_progress(med: dict) -> int:
    """
    Прогресс (%):
    Курс:       taken_doses / (course_days * doses_per_day) * 100
    Пожизненно: % израсходованного от initial_units
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
        stock  = calc_stock_days(med)
        taken  = med.get("taken_doses", 0) or 0
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

def format_schedule(times: dict) -> str:
    if not isinstance(times, dict) or not times:
        return "не указано"
    if times.get("Everyday"):
        return f"Каждый день: {', '.join(times['Everyday'])}"
    dn    = {"0":"Пн","1":"Вт","2":"Ср","3":"Чт","4":"Пт","5":"Сб","6":"Вс"}
    parts = [f"{dn.get(d,d)}: {', '.join(times[d])}" for d in sorted(times) if times.get(d)]
    return " | ".join(parts) if parts else "не указано"

def build_med_row(name: str, med: dict, chat_id: int) -> dict:
    form             = med.get("form", "tablets")
    cd               = med.get("course_days") or 0
    is_started       = med.get("is_started", False)
    days_left        = calc_days_left_in_course(med)
    stock_days       = calc_stock_days(med)
    progress         = calc_progress(med)
    rem_units        = calc_remaining_units(med)
    daily_units      = calc_daily_units(med)
    daily_mg         = med.get("daily_mg", 0)
    unit_mg          = med.get("unit_mg", 0)
    unit_name        = get_unit_name(form)
    schedule         = format_schedule(med.get("times", {}))
    course_done      = is_course_finished(med)
    stock_empty      = is_stock_empty(med)
    need_refill      = stock_empty and is_started and not course_done

    end_date         = None
    course_days_left = None
    is_enough        = True

    if cd > 0 and is_started:
        course_days_left = days_left
        is_enough        = stock_days >= course_days_left
        start = _parse_dt(med.get("start_date"))
        if start:
            end_date = (start + timedelta(days=cd)).strftime("%d.%m.%Y")

    return {
        "name":             name,
        "chat_id":          chat_id,
        "form":             form,
        "daily_mg":         daily_mg,
        "unit_mg":          unit_mg,
        "total_mg":         med.get("total_mg", 0),
        "remaining_units":  round(rem_units, 2),
        "daily_units":      round(daily_units, 2),
        "unit_name":        unit_name,
        "dose_line":        f"{daily_mg} мг/сут",
        "course_days":      cd,
        "is_started":       is_started,
        "progress":         progress,
        "taken_doses":      med.get("taken_doses", 0) or 0,
        "days_left":        days_left,
        "stock_days":       stock_days,
        "is_enough":        is_enough,
        "end_date":         end_date,
        "course_days_left": course_days_left,
        "course_done":      course_done,
        "need_refill":      need_refill,
        "stock_empty":      stock_empty,
        "schedule":         schedule,
        "times":            med.get("times", {}),
    }

# ══════════════════════════════════════════════════════
#  API ЭНДПОИНТЫ
# ══════════════════════════════════════════════════════
@app.get("/api/meds")
async def get_meds(chat_id: Optional[int] = None):
    logger.info("GET /api/meds")
    store = _load()
    if chat_id is None:
        chat_id = next(iter(store), None)
        if chat_id is None:
            return []
    result = [build_med_row(n, m, chat_id) for n, m in store.get(chat_id, {}).items()]
    logger.info(f"  → {len(result)} записей")
    return result


@app.post("/api/meds/add")
async def add_medicine(req: AddMedicineRequest):
    logger.info(f"POST /api/meds/add  name={req.name}")
    try:
        store    = _load()
        chat_id  = req.chat_id or next(iter(store), 12345)
        req_name = req.name.strip()
        if not req_name:
            raise HTTPException(400, "Название не может быть пустым")
        if req_name in store.get(chat_id, {}):
            raise HTTPException(400, "Лекарство с таким названием уже существует")

        # БАГ №3 ИСПРАВЛЕН: calc_total_from_units правильно считает liquid
        total = calc_total_from_units(req.form, req.unit_mg, req.units)

        store.setdefault(chat_id, {})[req_name] = {
            "form":              req.form,
            "daily_mg":          req.daily_mg,
            "unit_mg":           req.unit_mg,
            "total_mg":          total,
            "initial_units":     req.units,
            "course_days":       req.course_days if req.course_days > 0 else None,
            "taken_doses":       0,
            "created":           get_now().isoformat(),
            "is_started":        False,
            "start_date":        None,
            "times":             {},
            "notified":          False,
            "last_reminder_key": None,
            "last_9am_key":      None,
        }

        if not _save(store):
            raise HTTPException(500, "Ошибка сохранения")

        med  = store[chat_id][req_name]
        un   = get_unit_name(req.form)
        rem  = round(calc_remaining_units(med), 1)
        dly  = round(calc_daily_units(med), 1)
        days = calc_days_left_in_course(med)

        return {
            "success": True,
            "message": (f"✅ Лекарство успешно добавлено!\n"
                        f"Запас: {rem} {un}. Расход: {req.daily_mg} мг/сут. "
                        f"Хватит на {days} дней."),
            "name": req_name
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/meds/update")
async def update_medicine(req: UpdateMedicineRequest):
    logger.info(f"POST /api/meds/update  {req.med_name}")
    try:
        store = _load()
        if req.chat_id not in store:
            raise HTTPException(404, "Чат не найден")
        if req.med_name not in store[req.chat_id]:
            raise HTTPException(404, "Лекарство не найдено")

        med = store[req.chat_id][req.med_name]
        un  = get_unit_name(med.get("form","tablets"))

        if req.daily_mg is not None:
            med["daily_mg"] = req.daily_mg
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left_in_course(med)
            return {"success": True, "message": f"✅ Дозировка: {req.daily_mg} мг/сут. Хватит на {days} дн."}

        if req.add_stock is not None:
            unit_mg = med.get("unit_mg", 1)
            added   = calc_total_from_units(med.get("form","tablets"), unit_mg, req.add_stock)
            med["total_mg"] += added
            med["notified"]  = False
            if not med.get("course_days"):
                med["initial_units"] = (med.get("initial_units") or 0) + req.add_stock
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left_in_course(med)
            rem  = round(calc_remaining_units(med), 1)
            return {"success": True, "message": f"🔄 Пополнено! Остаток: {rem} {un}. Хватит на {days} дн."}

        raise HTTPException(400, "Нечего обновлять")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/meds/start")
async def start_course(req: StartCourseRequest):
    logger.info(f"POST /api/meds/start  {req.med_name}")
    try:
        store = _load()
        if req.chat_id not in store:
            raise HTTPException(404, "Чат не найден")
        if req.med_name not in store[req.chat_id]:
            raise HTTPException(404, "Лекарство не найдено")
        med = store[req.chat_id][req.med_name]
        med["is_started"]  = True
        med["start_date"]  = get_now().isoformat()
        med["taken_doses"] = 0
        med["notified"]    = False
        if not _save(store):
            raise HTTPException(500, "Ошибка сохранения")
        return {"success": True, "message": f"▶️ Курс «{req.med_name}» начат!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.post("/api/taken")
async def mark_taken(req: TakenRequest):
    """
    БАГ №1 ИСПРАВЛЕН: данные записываются в тот же DATA_FILE.
    Вычитает одну таблетку (unit_mg), инкрементирует taken_doses.
    Возвращает updated-поля для мгновенного обновления шкалы.
    """
    logger.info(f"POST /api/taken  {req.med_name}")
    try:
        store       = _load()
        updated     = {}
        course_done = False
        need_refill = False

        if req.chat_id in store and req.med_name in store[req.chat_id]:
            med = store[req.chat_id][req.med_name]

            # вычитаем одну таблетку (unit_mg), а не суточную дозу
            unit_mg = med.get("unit_mg") or med.get("daily_mg") or 1
            med["total_mg"]    = max(0, (med.get("total_mg") or 0) - unit_mg)
            med["taken_doses"] = (med.get("taken_doses") or 0) + 1

            # БАГ №1: _save записывает в тот же DATA_FILE что и бот
            _save(store)

            course_done = is_course_finished(med)
            need_refill = is_stock_empty(med) and med.get("is_started") and not course_done

            updated = {
                "total_mg":        round(med["total_mg"], 4),
                "remaining_units": round(calc_remaining_units(med), 2),
                "daily_units":     round(calc_daily_units(med), 2),
                "days_left":       calc_days_left_in_course(med),
                "stock_days":      calc_stock_days(med),
                "progress":        calc_progress(med),
                "taken_doses":     med["taken_doses"],
                "course_done":     course_done,
                "need_refill":     need_refill,
                "stock_empty":     is_stock_empty(med),
            }

        if course_done:
            msg = "🎉 Курс завершён!"
        elif need_refill:
            msg = "⚠️ Таблетки закончились! Пополните запас."
        else:
            msg = "✅ Приём отмечен!"

        return {"success": True, "message": msg, "updated": updated}

    except Exception as e:
        logger.error(e)
        return {"success": True, "message": "✅ Приём отмечен!", "updated": {}}


@app.post("/api/meds/schedule")
async def set_schedule(req: ScheduleRequest):
    store = _load()
    if req.chat_id not in store or req.med_name not in store[req.chat_id]:
        raise HTTPException(404, "Лекарство не найдено")

    times = [
        t.strip()
        for t in req.times
        if isinstance(t, str) and re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", t.strip())
    ]
    if not times:
        raise HTTPException(400, "Нужно передать хотя бы одно корректное время в формате HH:MM")

    med = store[req.chat_id][req.med_name]
    if not isinstance(med.get("times"), dict):
        med["times"] = {}
    med["times"][req.day_key] = sorted(set(times))
    med["last_reminder_key"] = None
    med["last_push_key"] = None

    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения")
    return {"success": True, "message": "Расписание сохранено", "times": med["times"]}


@app.post("/api/meds/snooze")
async def snooze_schedule(req: SnoozeRequest):
    store = _load()
    if req.chat_id not in store or req.med_name not in store[req.chat_id]:
        raise HTTPException(404, "Лекарство не найдено")
    if req.minutes <= 0:
        raise HTTPException(400, "minutes должен быть больше 0")

    med = store[req.chat_id][req.med_name]
    med["snooze_until"] = (get_now() + timedelta(minutes=req.minutes)).strftime("%Y-%m-%d %H:%M")
    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения")
    return {"success": True, "message": f"Напоминание отложено на {req.minutes} мин"}


@app.delete("/api/meds")
async def delete_medicine(chat_id: int, med_name: str):
    logger.info(f"DELETE /api/meds  {med_name}")
    try:
        store = _load()
        if chat_id in store and med_name in store[chat_id]:
            del store[chat_id][med_name]
            if _save(store):
                return {"success": True, "message": "🗑 Лекарство удалено"}
        raise HTTPException(404, "Лекарство не найдено")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(500, str(e))


@app.get("/api/version")
async def get_version():
    return {"version": APP_VERSION}

@app.get("/health")
async def health():
    return {"status":"ok","version":APP_VERSION,"data_file":str(DATA_FILE),"exists":DATA_FILE.exists()}

@app.get("/test")
async def test():
    return {"message":"Server is working!","version":APP_VERSION}

@app.get("/")
async def root():
    p = STATIC_DIR / "index.html"
    return FileResponse(p, media_type='text/html', headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0'}) if p.exists() else {"status":"ok","error":"index.html not found"}

@app.get("/service-worker.js")
async def service_worker():
    p = STATIC_DIR / "service-worker.js"
    return FileResponse(p, media_type='application/javascript', headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0','Service-Worker-Allowed':'/'}) if p.exists() else {"status":"ok","error":"service-worker.js not found"}


VAPID_PRIVATE_KEY = "sHaxRrXHj95RPhQh0hXgRasfwgIYaGuybHJAVzdzAgk"
VAPID_PUBLIC_KEY = "BBqYJditjsv4ZXeaQvjX4irpgLjYdBxovGtAfjKMEfAmlZRy5LdQVPk6i755jyCUjvrB2r0oEX-Mhxx8Mes7NFI"
VAPID_CLAIMS = {"sub": "mailto:admin@example.com"}

def _send_web_push(subscription: dict, title: str, body: str) -> bool:
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS,
        )
        return True
    except WebPushException as e:
        logger.warning(f"push send failed: {e}")
        return False

def send_push_for_due_medicines() -> int:
    now = get_now()
    now_hhmm = now.strftime("%H:%M")
    weekday = str(now.weekday())
    day_key = now.strftime("%Y-%m-%d")
    sent = 0

    store = _load()
    subscriptions = store.get("_push_subscriptions", []) if isinstance(store, dict) else []
    if not subscriptions:
        return 0

    for chat_id, meds in store.items():
        if isinstance(chat_id, str):
            continue
        for med_name, med in meds.items():
            if not med.get("is_started"):
                continue

            snooze_until = _parse_dt(med.get("snooze_until"))
            if snooze_until and snooze_until > now:
                continue

            times = med.get("times", {})
            today_times = []
            if isinstance(times.get("Everyday"), list):
                today_times.extend(times.get("Everyday", []))
            if now.weekday() >= 5 and isinstance(times.get("Weekends"), list):
                today_times.extend(times.get("Weekends", []))
            if now.weekday() < 5 and isinstance(times.get("Weekdays"), list):
                today_times.extend(times.get("Weekdays", []))
            if isinstance(times.get(weekday), list):
                today_times.extend(times.get(weekday, []))

            if now_hhmm not in set(today_times):
                continue

            reminder_key = f"{day_key}:{now_hhmm}"
            if med.get("last_push_key") == reminder_key:
                continue

            title = "💊 Напоминание о приёме"
            body = f"Пора принять: {med_name}"
            for sub in subscriptions:
                if _send_web_push(sub, title, body):
                    sent += 1

            med["last_push_key"] = reminder_key

    _save(store)
    return sent

async def _push_reminder_loop():
    while True:
        try:
            sent = send_push_for_due_medicines()
            if sent:
                logger.info(f"sent {sent} web push reminder(s)")
        except Exception as e:
            logger.error(f"push reminder loop error: {e}")
        await asyncio.sleep(30)


@app.on_event("startup")
async def start_push_reminder_loop():
    asyncio.create_task(_push_reminder_loop())


@app.post("/api/subscribe")
async def subscribe_push(subscription: PushSubscriptionRequest):
    store = _load()
    subs = store.get("_push_subscriptions", [])
    if not any(item.get("endpoint") == subscription.endpoint for item in subs):
        subs.append(subscription.model_dump())
    store["_push_subscriptions"] = subs
    if not _save(store):
        raise HTTPException(500, "Ошибка сохранения push-подписки")
    return {"success": True, "message": "Push-подписка сохранена", "public_key": VAPID_PUBLIC_KEY}

@app.post("/api/push/check")
async def trigger_push_check():
    sent = send_push_for_due_medicines()
    return {"success": True, "sent": sent}


# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════
def run_server():
    port = int(os.getenv("PORT", 3000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"\n{'='*55}")
    print(f"🚀  MedTracker API  v{APP_VERSION}")
    print(f"📁  данные:  {DATA_FILE.absolute()}")
    print(f"📄  HTML:    {STATIC_DIR / 'index.html'}")
    print(f"🌐  http://{host}:{port}")
    print(f"{'='*55}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    run_server()
