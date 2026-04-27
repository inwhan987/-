#!/bin/bash
set -e

cd /stock-bot

BEFORE=$(git rev-parse HEAD)
git pull
AFTER=$(git rev-parse HEAD)

# 새 커밋 없으면 완전 스킵 (.env.overrides 변경은 볼륨 마운트 + 핫리로드로 자동 반영)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "[update] no new commits, skipping"
  exit 0
fi

# 소스코드·의존성이 바뀐 경우에만 재빌드
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
if echo "$CHANGED" | grep -qE '^(stock_bot/|main\.py|requirements\.txt|Dockerfile)'; then
  echo "[update] source changed — rebuilding"
  docker compose up -d --build stock-bot stock-web
else
  echo "[update] config/env only — restarting without rebuild"
  docker compose up -d stock-bot stock-web
fi
