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

# Открываем порт веб-интерфейса
EXPOSE 3000

# Запуск обоих процессов через supervisord
CMD ["supervisord", "-c", "/app/supervisord.conf"]
FROM python:3.11-slim

# Install supervisor to run multiple processes
RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose web server port
EXPOSE 3000

# Run both processes via supervisord
CMD ["supervisord", "-c", "/app/supervisord.conf"]
