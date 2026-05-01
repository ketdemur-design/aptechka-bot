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
