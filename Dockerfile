FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Предсоздаём файлы, которые монтируются с хоста,
# чтобы docker не создавал вместо них папки
RUN touch runtime_data.json messages.db statistics.db

CMD ["python", "bot.py"]
