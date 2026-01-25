import os
import math
from flask import Flask, request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =====================
# НАСТРОЙКИ
# =====================

TOKEN = os.getenv("BOT_TOKEN")

app = Flask(__name__)
telegram_app = Application.builder().token(TOKEN).build()

# =====================
# ПАМЯТЬ (ПОКА ВРЕМЕННАЯ)
# =====================

data_store = {}  # chat_id -> лекарства
user_states = {}  # chat_id -> этап ввода

# =====================
# ВСПОМОГАТЕЛЬНЫЕ СЛОВАРИ
# =====================

FORMS = {
    "tablets": "таблетки",
    "capsules": "капсулы",
    "sachets": "саше",
}

UNIT_QUESTION = {
    "tablets": "Сколько мг в одной таблетке?",
    "capsules": "Сколько мг в одной капсуле?",
    "sachets": "Сколько мг в одном саше?",
}

UNIT_NAME = {
    "tablets": "таблеток",
    "capsules": "капсул",
    "sachets": "саше",
}

# =====================
# /start
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Добавить лекарство", callback_data="add")],
        [InlineKeyboardButton("🔄 Пополнить лекарство", callback_data="refill")],
    ]
    await update.message.reply_text(
        "Привет 👋\nЯ помогу следить за лекарствами.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

telegram_app.add_handler(CommandHandler("start", start))

# =====================
# КНОПКИ
# =====================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "add":
        user_states[chat_id] = {"step": "name"}
        await query.message.reply_text("Введите название лекарства:")

    elif data == "refill":
        meds = data_store.get(chat_id, {})
        if not meds:
            await query.message.reply_text("Пока нет лекарств.")
            return

        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"refill_{name}")]
            for name in meds
        ]
        await query.message.reply_text(
            "Что вы докупили?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("form_"):
        form = data.split("_")[1]
        user_states[chat_id]["form"] = form
        user_states[chat_id]["step"] = "mg_per_unit"
        await query.message.reply_text(UNIT_QUESTION[form])

    elif data.startswith("refill_"):
        name = data.replace("refill_", "")
        user_states[chat_id] = {
            "step": "refill_amount",
            "name": name,
        }
        form = data_store[chat_id][name]["form"]
        await query.message.reply_text(
            f"Сколько {UNIT_NAME[form]} вы купили?"
        )

telegram_app.add_handler(CallbackQueryHandler(buttons))

# =====================
# ТЕКСТОВЫЙ ВВОД
# =====================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text

    state = user_states.get(chat_id)
    if not state:
        return

    try:
        # Название
        if state["step"] == "name":
            state["name"] = text
            state["step"] = "form"
            keyboard = [
                [InlineKeyboardButton(v, callback_data=f"form_{k}")]
                for k, v in FORMS.items()
            ]
            await update.message.reply_text(
                "Выберите форму лекарства:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        # мг в единице
        elif state["step"] == "mg_per_unit":
            state["mg_per_unit"] = float(text.replace(",", "."))
            state["step"] = "count"
            await update.message.reply_text(
                f"Сколько {UNIT_NAME[state['form']]} вы купили?"
            )

        # количество
        elif state["step"] == "count":
            count = float(text.replace(",", "."))
            total_mg = count * state["mg_per_unit"]

            data_store.setdefault(chat_id, {})[state["name"]] = {
                "form": state["form"],
                "total_mg": total_mg,
            }

            await update.message.reply_text(
                f"✅ Лекарство «{state['name']}» добавлено.\n"
                f"Всего: {math.floor(total_mg)} мг"
            )
            user_states.pop(chat_id)

        # пополнение
        elif state["step"] == "refill_amount":
            count = float(text.replace(",", "."))
            med = data_store[chat_id][state["name"]]
            added = count * med["total_mg"] / med["total_mg"]
            med["total_mg"] += added

            await update.message.reply_text(
                f"🔄 Лекарство «{state['name']}» пополнено."
            )
            user_states.pop(chat_id)

    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число.")

telegram_app.add_handler(CommandHandler("text", text_handler))
telegram_app.add_handler(
    telegram.ext.MessageHandler(
        telegram.ext.filters.TEXT & ~telegram.ext.filters.COMMAND,
        text_handler,
    )
)

# =====================
# WEBHOOK
# =====================

@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "ok"


@app.route("/")
def index():
    return "Bot is running"






