#!/bin/bash
# auto-update: git pull --rebase + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# ── .env.overrides 잠금 우회 ────────────────────────────────────────────────
# Windows/Linux 공통: 봇 프로세스가 파일을 열어두고 있어
# git rebase 가 unlink → "Device or resource busy" 발생.
# → 파일 내용 저장 후 rename 으로 물리적으로 비워둠 (열린 파일도 rename 가능).
# → pull 완료 후 저장한 내용 복원.
_OVR=".env.overrides"
_OVR_BAK=".env.overrides.rebase_bak"
_OVR_CONTENT=""

# 이전 비정상 종료 잔여물 정리
[ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"

if [ -f "$_OVR" ]; then
  _OVR_CONTENT=$(cat "$_OVR")
  mv "$_OVR" "$_OVR_BAK"
  echo "[update] .env.overrides 임시 이동"
fi

# ── git pull --rebase ────────────────────────────────────────────────────────
BEFORE=$(git rev-parse HEAD)

git fetch origin main
git rebase --autostash -X theirs origin/main || {
  echo "[update] rebase 실패, abort 후 종료"
  git rebase --abort 2>/dev/null || true
  # .env.overrides 복원
  if [ -n "$_OVR_CONTENT" ]; then
    printf '%s' "$_OVR_CONTENT" > "$_OVR"
    [ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"
  fi
  exit 1
}

AFTER=$(git rev-parse HEAD)

# ── .env.overrides 복원 ──────────────────────────────────────────────────────
if [ -n "$_OVR_CONTENT" ]; then
  printf '%s' "$_OVR_CONTENT" > "$_OVR"
  git add "$_OVR"
  echo "[update] .env.overrides 복원 완료"
fi
[ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"

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
