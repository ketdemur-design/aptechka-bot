import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
TZ_MOSCOW = pytz.timezone("Europe/Moscow")
BOT_VERSION = "1.1.26"
DATA_FILE = Path(os.getenv("DATA_FILE", "meds_data.json"))

app = FastAPI(title="MedBot Web API")

# Asyncio lock — shared file writer safety
_write_lock = asyncio.Lock()

DAYS_MAP = {
    "Everyday": "Каждый день",
    "Weekdays": "Будни (Пн-Пт)",
    "Weekends": "Выходные (Сб-Вс)",
    "0": "Пн", "1": "Вт", "2": "Ср", "3": "Чт",
    "4": "Пт", "5": "Сб", "6": "Вс",
}

# ── Data helpers ──────────────────────────────────────────────────────────────

def get_now():
    return datetime.now(TZ_MOSCOW)


def get_display_units(med: dict) -> tuple[str, str]:
    form = med.get("form", "tablets")
    if form == "drops":
        return "мл", "капель"
    if form == "spray":
        return "мл", "впрыскиваний"
    if form == "liquid":
        return "мл", "мл"
    return "мг", "мг"


def calc_days_left(med: dict) -> int:
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
        except Exception:
            return capacity_days
    now_dt = get_now()
    days_passed = (now_dt - start_dt).days
    return max(0, capacity_days - days_passed)


def format_schedule(times_dict: dict) -> str:
    if not isinstance(times_dict, dict):
        return "не установлено"
    if times_dict.get("Everyday"):
        return "Каждый день: " + ", ".join(times_dict["Everyday"])
    lines = []
    for key in sorted(times_dict):
        if times_dict[key]:
            lines.append(f"{DAYS_MAP.get(key, key)}: {', '.join(times_dict[key])}")
    return "\n".join(lines) if lines else "не установлено"


def _deserialize_med(med: dict) -> dict:
    payload = dict(med)
    payload.setdefault("times", {})
    payload.setdefault("notified", False)
    payload.setdefault("is_started", False)
    payload.setdefault("last_reminder_key", None)
    return payload


def _serialize_med(med: dict) -> dict:
    payload = dict(med)
    for dt_key in ("created", "start_date"):
        if isinstance(payload.get(dt_key), datetime):
            payload[dt_key] = payload[dt_key].isoformat()
    return payload


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        raw_text = DATA_FILE.read_text(encoding="utf-8").strip()
        if not raw_text:
            return {}
        raw = json.loads(raw_text)
        result = {}
        for chat_id, meds in raw.items():
            result[str(chat_id)] = {
                name: _deserialize_med(med) for name, med in meds.items()
            }
        return result
    except Exception as e:
        print(f"[web_app] load error: {e}")
        return {}


async def save_data(data_store: dict):
    serializable = {
        str(cid): {name: _serialize_med(med) for name, med in meds.items()}
        for cid, meds in data_store.items()
    }
    async with _write_lock:
        temp = DATA_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(DATA_FILE)


# ── Models ────────────────────────────────────────────────────────────────────

class TakenRequest(BaseModel):
    chat_id: str
    med_name: str


# ── API Routes ────────────────────────────────────────────────────────────────

def calc_remaining_doses(med: dict) -> float:
    """
    Return the current remaining stock expressed in the user-facing unit
    (капли / впрыскивания / мл / мг).

    Internally app.py stores total_mg as:
      drops  → total капель  (1 капля  = 0.05 мл → объём/0.05 * флаконов)
      spray  → total впрыскиваний (1 впрыскивание = 0.1 мл → объём/0.1 * флаконов)
      liquid → total мл
      *      → total мг
    daily_mg is stored in the same unit, so total_mg/daily_mg = days.
    We just return total_mg rounded for display.
    """
    return round(med.get("total_mg", 0), 2)


def human_dose_line(med: dict) -> str:
    """
    Return a human-readable 'X единиц/сутки · Y мл/флакон' line
    that mirrors the bot's own display logic.
    """
    form      = med.get("form", "tablets")
    daily     = med.get("daily_mg", 0)
    unit_size = med.get("unit_mg", 0)          # объём флакона в мл (для drops/spray)

    if form == "drops":
        # daily_mg = капли/сутки; unit_mg = мл/флакон
        ml_per_day = daily * 0.05
        return f"{daily:g} кап/сутки · {unit_size:g} мл/флакон"
    if form == "spray":
        # daily_mg = впрыскиваний/сутки; unit_mg = мл/флакон
        ml_per_day = daily * 0.1
        return f"{daily:g} впрыск/сутки · {unit_size:g} мл/флакон"
    if form == "liquid":
        return f"{daily:g} мл/сутки · {unit_size:g} мл/ед."
    # tablets / capsules / sachet
    return f"{daily:g} мг/сутки · {unit_size:g} мг/ед."


@app.get("/api/meds")
def get_all_meds():
    """Return all medications across all chat_ids for the web view."""
    data = load_data()
    result = []
    for chat_id, meds in data.items():
        for name, med in meds.items():
            unit_label, dose_label = get_display_units(med)
            days_left  = calc_days_left(med)
            total      = med.get("total_mg", 0)
            daily      = med.get("daily_mg", 1) or 1
            form       = med.get("form", "tablets")

            # capacity_days = total запас / суточный расход (обе величины в одних единицах)
            capacity       = int(total // daily) if daily > 0 else 0
            consumed_days  = max(0, capacity - days_left)
            progress       = round((consumed_days / capacity * 100), 1) if capacity > 0 else 0

            remaining = calc_remaining_doses(med)

            result.append({
                "chat_id":      chat_id,
                "name":         name,
                "form":         form,
                "unit_mg":      med.get("unit_mg", 0),
                "total_mg":     total,
                "daily_mg":     daily,
                "unit_label":   unit_label,
                "dose_label":   dose_label,
                # Остаток в единицах хранения (капли/впрыскивания/мл/мг)
                "remaining_doses": remaining,
                # Строка для отображения в карточке
                "dose_line":    human_dose_line(med),
                "days_left":    days_left,
                "course_days":  med.get("course_days"),
                "is_started":   med.get("is_started", False),
                "schedule":     format_schedule(med.get("times", {})),
                "progress":     progress,
                "capacity_days": capacity,
            })
    return result


@app.post("/api/taken")
async def mark_taken(req: TakenRequest):
    """Reduce the stock by one daily dose."""
    data = load_data()
    chat_id = str(req.chat_id)
    if chat_id not in data or req.med_name not in data[chat_id]:
        raise HTTPException(status_code=404, detail="Лекарство не найдено")

    med = data[chat_id][req.med_name]
    daily = med.get("daily_mg", 0)
    if daily <= 0:
        raise HTTPException(status_code=400, detail="Неверная дозировка")

    if med["total_mg"] < daily:
        raise HTTPException(status_code=400, detail="Запас уже исчерпан")

    med["total_mg"] = round(med["total_mg"] - daily, 4)
    await save_data(data)

    days_left = calc_days_left(med)
    _, dose_label = get_display_units(med)
    return {
        "ok": True,
        "new_total": med["total_mg"],
        "days_left": days_left,
        "dose_label": dose_label,
    }


@app.get("/api/version")
def version():
    return {"version": BOT_VERSION}


# ── Static files (PWA) ────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/service-worker.js")
def sw():
    return FileResponse(str(STATIC_DIR / "service-worker.js"), media_type="application/javascript")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # Берем порт, который дал хостинг (3000), или 3000 по умолчанию
    port = int(os.getenv("PORT", 3000))
    uvicorn.run("web_app:app", host="0.0.0.0", port=port, reload=False)
