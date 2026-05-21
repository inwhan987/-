#!/bin/bash
# auto-update: git pull --rebase + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# ── .env.overrides: git 버전 그대로 사용 ────────────────────────────────────
# PC(또는 Claude)가 git에 push한 값이 진실의 원천.
# rebase 중 파일잠금 방지를 위해 임시 이동 후 rebase 완료 시 git 버전 유지.
_OVR=".env.overrides"
_OVR_BAK=".env.overrides.rebase_bak"

# 이전 비정상 종료 잔여물 정리
[ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"

if [ -f "$_OVR" ]; then
  mv "$_OVR" "$_OVR_BAK"
  echo "[update] .env.overrides 임시 이동"
fi

# ── git pull --rebase ────────────────────────────────────────────────────────
BEFORE=$(git rev-parse HEAD)

git fetch origin main
git rebase --autostash origin/main || {
  echo "[update] rebase 실패, abort 후 종료"
  git rebase --abort 2>/dev/null || true
  # 실패 시에만 봇 버전 복원
  [ -f "$_OVR_BAK" ] && mv "$_OVR_BAK" "$_OVR"
  exit 1
}

AFTER=$(git rev-parse HEAD)

# rebase 성공: git 버전 사용 (이미 _OVR 위치에 checkout 됨)
[ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"
echo "[update] .env.overrides git 버전 적용 완료"

# ── 새 커밋 없으면 스킵 ─────────────────────────────────────────────────────
if [ "$BEFORE" = "$AFTER" ]; then
  echo "[update] no new commits, skipping docker restart"
  exit 0
fi

echo "[update] new commits: $BEFORE → $AFTER"

# ── docker 재시작 ────────────────────────────────────────────────────────────
docker compose stop stock-bot stock-web 2>/dev/null || true
docker compose rm -f stock-bot stock-web 2>/dev/null || true

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
if echo "$CHANGED" | grep -qE '^(requirements\.txt|Dockerfile)'; then
  echo "[update] dependencies changed — rebuilding"
  docker compose up -d --build stock-bot stock-web
else
  echo "[update] code/config changed — restarting"
  docker compose up -d stock-bot stock-web
fi
