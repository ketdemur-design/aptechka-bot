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

from settings import APP_VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════
TZ_MOSCOW  = pytz.timezone('Europe/Moscow')
DATA_FILE  = Path("meds_data.json")
STATIC_DIR = Path(__file__).parent / "static"

logger.info(f"📁 server.py  данные: {DATA_FILE.absolute()}")

app = FastAPI(title="MedTracker", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ══════════════════════════════════════════════════════
#  PYDANTIC-МОДЕЛИ
# ══════════════════════════════════════════════════════
class AddMedicineRequest(BaseModel):
    chat_id:     Optional[int] = 0
    name:        str
    form:        str
    unit_mg:     float
    units:       float
    daily_mg:    float   # уже в «внутренних мг» (daily_units * unit_mg)
    course_days: int = 0

class UpdateMedicineRequest(BaseModel):
    chat_id:  int
    med_name: str
    daily_mg:  Optional[float] = None  # новый суточный расход в мг
    add_stock: Optional[float] = None  # добавить N штук

class StartCourseRequest(BaseModel):
    chat_id:  int
    med_name: str

class TakenRequest(BaseModel):
    chat_id:  int
    med_name: str

# ══════════════════════════════════════════════════════
#  РАБОТА С ФАЙЛОМ ДАННЫХ
# ══════════════════════════════════════════════════════
def get_now() -> datetime:
    return datetime.now(TZ_MOSCOW)

def _load() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = DATA_FILE.read_text(encoding="utf-8").strip()
        return {int(k): v for k, v in json.loads(raw).items()} if raw else {}
    except Exception as e:
        logger.error(f"load error: {e}")
        return {}

def _save(store: dict) -> bool:
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
    Преобразует (количество единиц × размер единицы) во внутренний total_mg.
    drops:  unit_mg = мл флакона, 0.05 мл = 1 капля  → total = (мл / 0.05) * кол-во_флаконов
    spray:  unit_mg = мл флакона, 0.10 мл = 1 впрыск → total = (мл / 0.10) * кол-во_флаконов
    прочие: total = unit_mg * units
    """
    if form == "drops":
        return (unit_mg / 0.05) * units
    if form == "spray":
        return (unit_mg / 0.10) * units
    return unit_mg * units

def get_unit_name(form: str) -> str:
    """Название единицы для отображения (штуки вместо мг)."""
    return {
        "tablets":  "табл.",
        "capsules": "капс.",
        "liquid":   "мл",
        "drops":    "кап.",
        "spray":    "впрыск.",
        "sachet":   "саше",
    }.get(form, "ед.")

def calc_remaining_units(med: dict) -> float:
    """
    Остаток в штуках.
    Формула: total_mg / unit_mg
    drops/spray: total_mg хранит число единиц (капель/впрыскиваний) — возвращаем напрямую.
    """
    form    = med.get("form", "tablets")
    total   = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg",  0) or 0
    if form in ("drops", "spray"):
        return total          # уже в каплях/впрыскиваниях
    return (total / unit_mg) if unit_mg > 0 else 0

def calc_daily_units(med: dict) -> float:
    """
    Суточный расход в штуках.
    Формула: daily_mg / unit_mg
    drops/spray: daily_mg уже в каплях/впрыскиваниях.
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
    Логика:
      1. Считаем сколько дней хватит ТЕКУЩЕГО остатка при суточном расходе:
         capacity = floor(total_mg / daily_mg)
      2. Если курс начат — вычитаем уже прошедшие дни (запас расходуется ежедневно):
         result = capacity - passed_days
    Итого: это "через сколько дней закончится запас с сегодня".
    """
    dmg = med.get("daily_mg") or 0
    if dmg <= 0:
        return 0
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

    Курс N дней:
      progress = прошедшие_дни / N * 100
      Смысл: сколько процентов курса уже пройдено.
      0% — только начали, 100% — курс завершён.

    Пожизненный (course_days == 0 или None):
      progress = (1 - остаток_в_штуках / начальный_запас_в_штуках) * 100
      Смысл: сколько процентов упаковки израсходовано.
      0% — полная упаковка, 100% — всё выпито.
      Если initial_units не сохранён — считаем через дни
      (прошло / (прошло + осталось)).
    """
    cd = med.get("course_days") or 0

    if cd > 0:
        # ── курсовой: % пройденных дней ──
        if not med.get("is_started") or not med.get("start_date"):
            return 0
        start = _parse_dt(med["start_date"])
        if not start:
            return 0
        passed = (get_now() - start).days
        return max(0, min(100, int(passed / cd * 100)))

    else:
        # ── пожизненный: % израсходованного ──
        init    = med.get("initial_units")
        unit_mg = med.get("unit_mg") or 0
        total   = med.get("total_mg") or 0

        if init and init > 0 and unit_mg > 0:
            rem_units = total / unit_mg
            return max(0, min(100, int((1 - rem_units / init) * 100)))

        # fallback: прошло / (прошло + осталось)
        dmg = med.get("daily_mg") or 0
        if not dmg or not med.get("is_started") or not med.get("start_date"):
            return 0
        start = _parse_dt(med["start_date"])
        if not start:
            return 0
        capacity = max(1, int(total // dmg))    # дней осталось сегодня
        passed   = (get_now() - start).days      # дней прошло
        horizon  = capacity + passed             # начальный запас в днях
        return max(0, min(100, int(passed / horizon * 100)))

def format_schedule(times: dict) -> str:
    if not isinstance(times, dict) or not times:
        return "не указано"
    if times.get("Everyday"):
        return f"Каждый день: {', '.join(times['Everyday'])}"
    dn    = {"0":"Пн","1":"Вт","2":"Ср","3":"Чт","4":"Пт","5":"Сб","6":"Вс"}
    parts = [f"{dn.get(d,d)}: {', '.join(times[d])}" for d in sorted(times) if times.get(d)]
    return " | ".join(parts) if parts else "не указано"

def build_med_row(name: str, med: dict, chat_id: int) -> dict:
    """Собирает полный словарь для ответа /api/meds."""
    form          = med.get("form", "tablets")
    cd            = med.get("course_days") or 0
    is_started    = med.get("is_started", False)
    days_left     = calc_days_left(med)
    progress      = calc_progress(med)
    rem_units     = calc_remaining_units(med)
    daily_units   = calc_daily_units(med)
    unit_name     = get_unit_name(form)
    schedule      = format_schedule(med.get("times", {}))

    # прогнозные поля
    end_date         = None
    course_days_left = None
    is_enough        = True

    if cd > 0 and is_started and med.get("start_date"):
        start = _parse_dt(med["start_date"])
        if start:
            passed           = max(0, (get_now() - start).days)
            course_days_left = max(0, cd - passed)
            end_date         = (start + timedelta(days=cd)).strftime("%d.%m.%Y")
            # хватит ли запаса до конца курса?
            is_enough        = days_left >= course_days_left

    return {
        # идентификация
        "name":             name,
        "chat_id":          chat_id,
        # форма
        "form":             form,
        # внутренние единицы (мг)
        "daily_mg":         med.get("daily_mg", 0),
        "unit_mg":          med.get("unit_mg",  0),
        "total_mg":         med.get("total_mg", 0),
        # штуки для отображения
        "remaining_units":  round(rem_units, 2),
        "daily_units":      round(daily_units, 2),
        "unit_name":        unit_name,
        "dose_line":        f"{round(daily_units, 2)} {unit_name}/сут",
        # курс
        "course_days":      cd,
        "is_started":       is_started,
        "days_left":        days_left,
        "progress":         progress,
        # прогноз
        "is_enough":        is_enough,
        "end_date":         end_date,
        "course_days_left": course_days_left,
        # расписание
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
        store   = _load()
        chat_id = req.chat_id or next(iter(store), 12345)

        req_name = req.name.strip()
        if not req_name:
            raise HTTPException(400, "Название не может быть пустым")
        if req_name in store.get(chat_id, {}):
            raise HTTPException(400, "Лекарство с таким названием уже существует")

        # total_mg — внутреннее хранение
        total = calc_total_from_units(req.form, req.unit_mg, req.units)

        store.setdefault(chat_id, {})[req_name] = {
            "form":              req.form,
            "daily_mg":          req.daily_mg,   # уже в «мг» (daily_units * unit_mg)
            "unit_mg":           req.unit_mg,
            "total_mg":          total,
            "initial_units":     req.units,      # запоминаем для % пожизненного прогресса
            "course_days":       req.course_days if req.course_days > 0 else None,
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
        days = calc_days_left(med)
        un   = get_unit_name(req.form)
        rem  = round(calc_remaining_units(med), 1)
        dly  = round(calc_daily_units(med), 2)

        return {
            "success": True,
            "message": f"✅ Лекарство успешно добавлено!\nЗапас: {rem} {un}. Расход: {dly} {un}/сут. Хватит на {days} дней.",
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
        un  = get_unit_name(med.get("form", "tablets"))

        if req.daily_mg is not None:
            # daily_mg уже в «внутренних мг»
            med["daily_mg"] = req.daily_mg
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left(med)
            dly  = round(calc_daily_units(med), 2)
            return {"success": True, "message": f"✅ Дозировка изменена! {dly} {un}/сут. Хватит на {days} дн."}

        if req.add_stock is not None:
            # add_stock — количество штук/единиц
            unit_mg = med.get("unit_mg", 1)
            added   = calc_total_from_units(med.get("form", "tablets"), unit_mg, req.add_stock)
            med["total_mg"] += added
            med["notified"]  = False
            # обновляем initial_units для корректного % пожизненного прогресса
            if not med.get("course_days"):
                med["initial_units"] = (med.get("initial_units") or 0) + req.add_stock
            if not _save(store):
                raise HTTPException(500, "Ошибка сохранения")
            days = calc_days_left(med)
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
        med["is_started"] = True
        med["start_date"] = get_now().isoformat()
        med["notified"]   = False

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
    Отмечает приём одной дозы.
    Возвращает updated-поля для мгновенного обновления шкалы на фронте
    без полного перезапроса /api/meds.
    """
    logger.info(f"POST /api/taken  {req.med_name}")
    try:
        store   = _load()
        updated = {}

        if req.chat_id in store and req.med_name in store[req.chat_id]:
            med = store[req.chat_id][req.med_name]

            # вычисляем дозу одного приёма
            t_dict = med.get("times", {})
            dc = 1
            for t in t_dict.values():
                if t:
                    dc = max(dc, len(t))
            per_dose = med["daily_mg"] / dc if dc > 0 else med["daily_mg"]

            med["total_mg"] = max(0, med["total_mg"] - per_dose)
            _save(store)

            # свежие данные для мгновенного обновления фронта
            updated = {
                "total_mg":       round(med["total_mg"], 4),
                "remaining_units": round(calc_remaining_units(med), 2),
                "daily_units":     round(calc_daily_units(med), 2),
                "days_left":       calc_days_left(med),
                "progress":        calc_progress(med),
            }

        return {"success": True, "message": "✅ Приём отмечен!", "updated": updated}

    except Exception as e:
        logger.error(e)
        return {"success": True, "message": "✅ Приём отмечен!", "updated": {}}


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
    return {
        "status":    "ok",
        "version":   APP_VERSION,
        "data_file": str(DATA_FILE),
        "exists":    DATA_FILE.exists(),
    }

@app.get("/test")
async def test():
    return {"message": "Server is working!", "version": APP_VERSION}

@app.get("/")
async def root():
    p = STATIC_DIR / "index.html"
    return FileResponse(p) if p.exists() else {"status": "ok", "error": "index.html not found"}

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
