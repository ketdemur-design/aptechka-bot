import os
import json
from datetime import datetime
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

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== КОНФИГУРАЦИЯ ==================
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

# Используем ТОТ ЖЕ файл, что и в app.py
DATA_FILE = Path("meds_data.json")

logger.info(f"📁 Server использует файл данных: {DATA_FILE.absolute()}")

app = FastAPI()

STATIC_DIR = Path(__file__).parent / "static"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ================== МОДЕЛИ ==================
class AddMedicineRequest(BaseModel):
    chat_id: Optional[int] = 0
    name: str
    form: str
    unit_mg: float
    units: float
    daily_mg: float
    course_days: int = 0

class UpdateMedicineRequest(BaseModel):
    chat_id: int
    med_name: str
    daily_mg: Optional[float] = None
    add_stock: Optional[float] = None

class StartCourseRequest(BaseModel):
    chat_id: int
    med_name: str

class TakenRequest(BaseModel):
    chat_id: int
    med_name: str

# ================== РАБОТА С ДАННЫМИ ==================
def get_now():
    return datetime.now(TZ_MOSCOW)

def load_data_store():
    """Загружает данные из ТОГО ЖЕ файла, что и бот"""
    if not DATA_FILE.exists():
        logger.warning(f"⚠️ Файл данных не найден: {DATA_FILE}")
        return {}
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return {}
            raw = json.loads(content)
            return {int(k): v for k, v in raw.items()}
    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON: {e}")
        return {}
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки данных: {e}")
        return {}

def save_data_store(data_store):
    """Сохраняет данные в ТОТ ЖЕ файл, что и бот"""
    try:
        serializable = {str(k): v for k, v in data_store.items()}
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Данные сохранены в {DATA_FILE}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")
        return False

def calc_days_left(med):
    if not med.get("daily_mg") or med["daily_mg"] <= 0:
        return 0
    capacity_days = int(med["total_mg"] // med["daily_mg"])
    
    if not med.get("is_started") or not med.get("start_date"):
        return capacity_days
    
    start_dt = med["start_date"]
    if isinstance(start_dt, str):
        try:
            start_dt = datetime.fromisoformat(start_dt)
            if start_dt.tzinfo is None:
                start_dt = TZ_MOSCOW.localize(start_dt)
        except:
            return capacity_days
    
    days_passed = (get_now() - start_dt).days
    return max(0, capacity_days - days_passed)

def get_display_units(med):
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    if form == "spray":
        return "мл", "впрыскиваний"
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"

def format_schedule(times_dict):
    if not times_dict or not isinstance(times_dict, dict):
        return "не указано"
    
    if times_dict.get("Everyday"):
        return f"Каждый день: {', '.join(times_dict['Everyday'])}"
    
    day_names = {"0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт", "4": "Пт", "5": "Сб", "6": "Вс"}
    lines = []
    for day in sorted(k for k in times_dict if times_dict[k]):
        lines.append(f"{day_names.get(day, day)}: {', '.join(times_dict[day])}")
    
    return ", ".join(lines) if lines else "не указано"

def calculate_progress(med):
    course_days = med.get("course_days")
    if not course_days or course_days <= 0:
        return 0
    if not med.get("is_started") or not med.get("start_date"):
        return 0
    
    start_dt = med["start_date"]
    if isinstance(start_dt, str):
        try:
            start_dt = datetime.fromisoformat(start_dt)
            if start_dt.tzinfo is None:
                start_dt = TZ_MOSCOW.localize(start_dt)
        except:
            return 0
    
    days_passed = (get_now() - start_dt).days
    progress = min(100, int((days_passed / course_days) * 100))
    return max(0, progress)

def get_unit_name(form):
    unit_map = {
        "tablets": "табл.",
        "capsules": "капс.",
        "liquid": "мл",
        "drops": "кап.",
        "spray": "впрыск."
    }
    return unit_map.get(form, "ед.")

def calculate_remaining_units(med):
    form = med.get("form", "tablets")
    total_mg = med.get("total_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if form in ("drops", "spray"):
        return total_mg
    if unit_mg <= 0:
        return 0
    return total_mg / unit_mg

def calculate_daily_units(med):
    daily_mg = med.get("daily_mg", 0) or 0
    unit_mg = med.get("unit_mg", 0) or 0
    if unit_mg <= 0:
        return 0
    return daily_mg / unit_mg

# ================== API ЭНДПОИНТЫ ==================
@app.get("/api/meds")
async def get_meds(chat_id: Optional[int] = None):
    """Получить все лекарства"""
    logger.info("GET /api/meds called")
    data_store = load_data_store()
    
    if chat_id is None:
        if data_store:
            chat_id = list(data_store.keys())[0]
        else:
            return []
    
    meds = data_store.get(chat_id, {})
    result = []
    
    for name, med in meds.items():
        days_left = calc_days_left(med)
        _, dose_label = get_display_units(med)
        progress = calculate_progress(med)
        schedule = format_schedule(med.get("times", {}))
        form = med.get("form", "tablets")
        remaining_units = calculate_remaining_units(med)
        daily_units = calculate_daily_units(med)
        unit_name = get_unit_name(form)
        course_days = med.get("course_days", 0) or 0
        is_enough = True
        if course_days > 0 and med.get("is_started") and med.get("start_date"):
            start_dt = med["start_date"]
            if isinstance(start_dt, str):
                try:
                    start_dt = datetime.fromisoformat(start_dt)
                    if start_dt.tzinfo is None:
                        start_dt = TZ_MOSCOW.localize(start_dt)
                except:
                    start_dt = None
            if start_dt:
                days_passed = max(0, (get_now() - start_dt).days)
                course_days_left = max(0, course_days - days_passed)
                is_enough = days_left >= course_days_left
        
        result.append({
            "name": name,
            "chat_id": chat_id,
            "form": form,
            "daily_mg": med.get("daily_mg", 0),
            "unit_mg": med.get("unit_mg", 0),
            "total_mg": med.get("total_mg", 0),
            "course_days": course_days,
            "is_started": med.get("is_started", False),
            "days_left": days_left,
            "dose_label": dose_label,
            "progress": progress,
            "schedule": schedule,
            "remaining_units": round(remaining_units, 2),
            "daily_units": round(daily_units, 2),
            "unit_name": unit_name,
            "is_enough": is_enough,
            "remaining_doses": int(med.get("total_mg", 0) / med.get("daily_mg", 1)) if med.get("daily_mg", 0) > 0 else 0,
            "dose_line": f"{med.get('daily_mg', 0)} {dose_label}/сут"
        })
    
    logger.info(f"Returning {len(result)} medicines")
    return result

@app.post("/api/meds/add")
async def add_medicine(req: AddMedicineRequest):
    """Добавить новое лекарство"""
    logger.info(f"POST /api/meds/add called with: {req.name}, {req.form}")
    try:
        data_store = load_data_store()
        chat_id = req.chat_id
        if chat_id in (None, 0):
            chat_id = next(iter(data_store), 12345)
        
        req_name = req.name.strip()
        if not req_name:
            raise HTTPException(status_code=400, detail="Название лекарства не может быть пустым")

        # Пересчёт общего запаса
        if req.form == "drops":
            total_resource = (req.unit_mg / 0.05) * req.units
        elif req.form == "spray":
            total_resource = (req.unit_mg / 0.1) * req.units
        else:
            total_resource = req.unit_mg * req.units
        
        logger.info(f"Total resource calculated: {total_resource}")
        
        # Проверяем, не существует ли уже лекарство
        if req_name in data_store.get(chat_id, {}):
            logger.warning(f"Medicine {req_name} already exists")
            raise HTTPException(status_code=400, detail="Лекарство с таким названием уже существует")
        
        # Создаем запись
        data_store.setdefault(chat_id, {})[req_name] = {
            "form": req.form,
            "daily_mg": req.daily_mg,
            "unit_mg": req.unit_mg,
            "total_mg": total_resource,
            "course_days": req.course_days if req.course_days > 0 else None,
            "created": get_now().isoformat(),
            "is_started": False,
            "start_date": None,
            "times": {},
            "notified": False,
            "last_reminder_key": None,
            "last_9am_key": None,
        }
        
        # Сохраняем
        if save_data_store(data_store):
            days = calc_days_left(data_store[chat_id][req_name])
            logger.info(f"✅ Added medicine: {req_name}, lasts {days} days")
            return {"success": True, "message": f"✅ Лекарство добавлено! Хватит на {days} дней", "name": req_name}
        else:
            logger.error("Failed to save data")
            raise HTTPException(status_code=500, detail="Ошибка сохранения данных")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding medicine: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/meds/update")
async def update_medicine(req: UpdateMedicineRequest):
    """Обновить дозировку или пополнить запас"""
    logger.info(f"POST /api/meds/update called for {req.med_name}")
    try:
        data_store = load_data_store()
        
        if req.chat_id not in data_store:
            raise HTTPException(status_code=404, detail="Чат не найден")
        if req.med_name not in data_store[req.chat_id]:
            raise HTTPException(status_code=404, detail="Лекарство не найдено")
        
        med = data_store[req.chat_id][req.med_name]
        
        if req.daily_mg is not None:
            med["daily_mg"] = req.daily_mg
            if save_data_store(data_store):
                days = calc_days_left(med)
                return {"success": True, "message": f"Дозировка изменена! Хватит на {days} дн."}
        
        if req.add_stock is not None:
            form = med.get("form", "tablets")
            if form == "drops":
                added = (med["unit_mg"] / 0.05) * req.add_stock
            elif form == "spray":
                added = (med["unit_mg"] / 0.1) * req.add_stock
            else:
                added = med["unit_mg"] * req.add_stock
            
            med["total_mg"] += added
            med["notified"] = False
            
            if save_data_store(data_store):
                days = calc_days_left(med)
                return {"success": True, "message": f"Пополнено! Хватит на {days} дн."}
        
        raise HTTPException(status_code=500, detail="Ошибка сохранения")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating medicine: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/meds/start")
async def start_course(req: StartCourseRequest):
    """Начать курс лекарства"""
    logger.info(f"POST /api/meds/start called for {req.med_name}")
    try:
        data_store = load_data_store()
        
        if req.chat_id not in data_store:
            raise HTTPException(status_code=404, detail="Чат не найден")
        if req.med_name not in data_store[req.chat_id]:
            raise HTTPException(status_code=404, detail="Лекарство не найдено")
        
        med = data_store[req.chat_id][req.med_name]
        med["is_started"] = True
        med["start_date"] = get_now().isoformat()
        
        if save_data_store(data_store):
            return {"success": True, "message": f"Курс «{req.med_name}» начат!"}
        else:
            raise HTTPException(status_code=500, detail="Ошибка сохранения")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting course: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/taken")
async def mark_taken(req: TakenRequest):
    """Отметить приём лекарства"""
    logger.info(f"POST /api/taken called for {req.med_name}")
    try:
        data_store = load_data_store()
        
        if req.chat_id in data_store and req.med_name in data_store[req.chat_id]:
            med = data_store[req.chat_id][req.med_name]
            
            times_dict = med.get("times", {})
            doses_count = 1
            for times in times_dict.values():
                if times:
                    doses_count = max(doses_count, len(times))
            
            per_dose = med["daily_mg"] / doses_count if doses_count > 0 else med["daily_mg"]
            med["total_mg"] = max(0, med["total_mg"] - per_dose)
            
            save_data_store(data_store)
            return {"success": True, "message": "Приём отмечен"}
        
        return {"success": True, "message": "Приём отмечен"}
        
    except Exception as e:
        logger.error(f"Error marking taken: {e}")
        return {"success": True, "message": "Приём отмечен"}

@app.delete("/api/meds")
async def delete_medicine(chat_id: int, med_name: str):
    """Удалить лекарство"""
    logger.info(f"DELETE /api/meds called for {med_name}")
    try:
        data_store = load_data_store()
        
        if chat_id in data_store and med_name in data_store[chat_id]:
            del data_store[chat_id][med_name]
            
            if save_data_store(data_store):
                return {"success": True, "message": "Лекарство удалено"}
        
        raise HTTPException(status_code=404, detail="Лекарство не найдено")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting medicine: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/version")
async def get_version():
    return {"version": APP_VERSION}

@app.get("/health")
async def health_check():
    """Проверка работоспособности"""
    return {"status": "ok", "data_file": str(DATA_FILE), "exists": DATA_FILE.exists()}

@app.get("/test")
async def test():
    """Тестовый эндпоинт"""
    return {"message": "Server is working!"}

# ================== ГЛАВНАЯ СТРАНИЦА ==================
@app.get("/")
async def root():
    """Возвращает главную страницу"""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    else:
        return {"status": "ok", "service": "MedTracker API", "version": "V.55", "error": "index.html not found"}

# ================== ЗАПУСК ==================
def run_server():
    port = int(os.getenv("PORT", 3000))
    host = os.getenv("HOST", "0.0.0.0")
    
    print(f"\n{'='*50}")
    print(f"🚀 MedTracker API Server")
    print(f"📁 Файл данных: {DATA_FILE.absolute()}")
    print(f"📄 HTML файл: {STATIC_DIR / 'index.html'}")
    print(f"🌐 Адрес: http://{host}:{port}")
    print(f"📊 Health check: http://{host}:{port}/health")
    print(f"🧪 Test endpoint: http://{host}:{port}/test")
    print(f"{'='*50}\n")
    
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    run_server()
