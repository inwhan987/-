#!/bin/bash
# auto-update: git pull + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# 웹 UI가 수정하는 값만 따로 저장 (나머지는 git 값 사용)
_ui_keys="INITIAL_CAPITAL_KRW TRADE_FEE_BUY_PCT TRADE_FEE_SELL_PCT PERF_START_DATE API_BUDGET_USD"
declare -A _ui_vals
for _k in $_ui_keys; do
  _v=$(grep "^${_k}=" .env.overrides 2>/dev/null | tail -1 | cut -d= -f2-)
  [ -n "$_v" ] && _ui_vals[$_k]="$_v"
done

BEFORE=$(git rev-parse HEAD)
git pull
AFTER=$(git rev-parse HEAD)

# 웹 UI 값 복원 (git에서 받은 나머지 값은 그대로 유지)
for _k in "${!_ui_vals[@]}"; do
  sed -i "s|^${_k}=.*|${_k}=${_ui_vals[$_k]}|" .env.overrides
done

# 새 커밋 없으면 완전 스킵
if [ "$BEFORE" = "$AFTER" ]; then
  echo "[update] no new commits, skipping"
  exit 0
fi

# 충돌 방지: 봇/웹 컨테이너만 정리 (prometheus/grafana 유지)
docker compose stop stock-bot stock-web 2>/dev/null || true
docker compose rm -f stock-bot stock-web 2>/dev/null || true

# requirements.txt / Dockerfile 변경 시에만 재빌드, 나머지는 재시작
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
if echo "$CHANGED" | grep -qE '^(requirements\.txt|Dockerfile)'; then
  echo "[update] dependencies changed — rebuilding"
  docker compose up -d --build stock-bot stock-web
else
  echo "[update] code/config changed — restarting"
  docker compose up -d stock-bot stock-web
fi
