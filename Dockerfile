FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata gcc git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY stock_bot ./stock_bot
COPY main.py ./

RUN mkdir -p /app/logs

CMD ["python", "main.py", "live"]
