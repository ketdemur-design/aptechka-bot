import os

# Единая версия для Telegram-бота и веб-интерфейса.
# Можно переопределить переменной окружения APP_VERSION.
APP_VERSION = os.getenv("APP_VERSION", "V.31")
