#!/bin/bash
# auto-update: git pull + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# .env.overrides 는 UI 핫리로드가 런타임에 수정하므로 pull 전에 stash
git stash --quiet 2>/dev/null || true

BEFORE=$(git rev-parse HEAD)
git pull
AFTER=$(git rev-parse HEAD)

# stash 복원 (런타임 변경값 유지)
git stash pop --quiet 2>/dev/null || true

# 새 커밋 없으면 완전 스킵
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
