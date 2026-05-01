FROM python:3.11-slim

# Install supervisor to run multiple processes
RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose web server port
EXPOSE 8000

# Run both processes via supervisord
CMD ["supervisord", "-c", "/app/supervisord.conf"]
# Используем официальный образ Python
FROM python:3.10-slim

# Устанавливаем рабочую папку
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все остальные файлы бота в контейнер
COPY . .

# Команда для запуска бота (так как ваш главный файл называется app.py)
CMD ["python", "app.py"]
