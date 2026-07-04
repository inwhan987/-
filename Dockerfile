FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata gcc git \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (LLM_BACKEND=claude_code 용 — 구독 기반, API 사용료 0).
# LLM_BACKEND=api(기본)일 땐 호출되지 않으므로 이미지에 있어도 동작 변화 없음.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

RUN mkdir -p /app/logs

CMD ["python", "main.py", "live"]
