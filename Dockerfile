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
