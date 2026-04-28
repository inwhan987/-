#!/bin/bash
# auto-update: git pull + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# .env.overrides 는 UI 핫리로드가 수정하므로 pull 전에 백업 후 복원
cp .env.overrides .env.overrides.bak 2>/dev/null || true
git checkout .env.overrides 2>/dev/null || true

BEFORE=$(git rev-parse HEAD)
git pull
AFTER=$(git rev-parse HEAD)

# 백업이 있으면 복원 (사용자 설정 우선)
if [ -f .env.overrides.bak ]; then
  cp .env.overrides.bak .env.overrides
  rm .env.overrides.bak
fi

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
