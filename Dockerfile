FROM python:3.11-slim

# Системные пакеты: supervisor для запуска нескольких процессов
RUN apt-get update && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходники
COPY . .

# Создаём директорию для данных (перезаписывается volume при запуске)
RUN mkdir -p /data

# Создаём директорию для статики
RUN mkdir -p /app/static

# Открываем порт веб-интерфейса
EXPOSE 3000

# Запуск обоих процессов через supervisord
CMD ["supervisord", "-c", "/app/supervisord.conf"]
