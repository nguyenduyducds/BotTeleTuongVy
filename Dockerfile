# Dockerfile — Build image cho Bot Telegram
FROM python:3.11-slim

WORKDIR /app

# Cài thư viện
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ code
COPY . .

# Tạo thư mục lưu database (sẽ được mount volume)
RUN mkdir -p /app/data

# Biến môi trường (sẽ được set trên Railway/Render)
ENV BOT_TOKEN=""
ENV TG_API_ID="0"
ENV TG_API_HASH=""
ENV DB_PATH="/app/data/bot_data.db"

CMD ["python", "bot.py"]
