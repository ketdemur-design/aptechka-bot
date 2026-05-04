async def save_medicine(update_or_query, chat_id):
    d = user_states[chat_id]["data"]
    form = d["form"]
    unit_mg = d["unit_mg"]
    units = d["units"]
    daily_mg = d["daily_mg"]
    course_days = d.get("course_days")

    # Пересчёт общего запаса в "мг" (условные единицы)
    if form == "drops":
        total_resource = (unit_mg / 0.05) * units
    elif form == "spray":
        total_resource = (unit_mg / 0.1) * units
    else:
        total_resource = unit_mg * units

    data_store.setdefault(chat_id, {})[d["name"]] = {
        "form": form,
        "daily_mg": daily_mg,
        "unit_mg": unit_mg,
        "total_mg": total_resource,
        "course_days": course_days,
        "created": get_now(),
        "is_started": False,
        "start_date": None,
        "times": {},
        "notified": False,
        "last_reminder_key": None,
    }

    await save_data_store_async()
    med = data_store[chat_id][d["name"]]
    days = calc_days_left(med)
    unit_label, dose_label = get_display_units(med)

    msg = f"✅ Лекарство *{d['name']}* успешно добавлено!\n\n"
    if form in ("drops", "spray", "liquid"):
        msg += f"Объем флакона/ед: {unit_mg:g} мл\n"
    else:
        msg += f"Дозировка ед: {unit_mg:g} мг\n"
    msg += (
        f"Расход: {daily_mg:g} {dose_label}/сутки\n"
        f"Хватит на: {days} дней\n\n"
        "⚠️ Нажмите «▶️ Начать курс» для старта отсчёта."
    )

    # Отправляем подтверждение в зависимости от типа входящего объекта
    if hasattr(update_or_query, 'reply_text'):
        # Это объект Message (из text_handler)
        await update_or_query.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu())
    else:
        # Это CallbackQuery (из кнопки "Пожизненно")
        await update_or_query.edit_message_text(msg, parse_mode="Markdown")
        # Отправляем новое сообщение с главным меню, потому что предыдущее было отредактировано
        await update_or_query.message.reply_text("Главное меню:", reply_markup=main_menu())

    user_states.pop(chat_id, None)
