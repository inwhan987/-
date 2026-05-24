#!/bin/bash
# auto-update: git pull --rebase + docker rebuild on source change
set -e

# ── 동시 실행 방지 (빌드가 1분 이상 걸릴 때 cron 중복 실행 차단) ────────────────
exec 9>/tmp/update_stock.lock
flock -n 9 || { echo "[update] already running, skipping"; exit 0; }

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

# rebase 성공: git HEAD 버전으로 명시적 복원 (mv로 비워뒀으므로 checkout 필요)
[ -f "$_OVR_BAK" ] && rm -f "$_OVR_BAK"
git checkout HEAD -- "$_OVR" 2>/dev/null || true
echo "[update] .env.overrides git 버전 적용 완료"

mkdir -p data   # 해시 파일 저장 디렉터리 보장

# ── 의존성 변경 감지 (커밋 여부와 무관하게 항상 체크) ──────────────────────────
# requirements.txt + Dockerfile 내용 해시를 마지막 빌드 시점과 비교.
# 수동 git pull 후 update.sh가 "no new commits"로 스킵해도 재빌드가 보장됨.
_HASH_FILE="data/.last_build_hash"
# 패키지 이름만 추출 (버전 제거) + Dockerfile → 새 패키지 추가·삭제·Dockerfile 변경 시만 재빌드
_CUR_HASH=$(grep -v '^\s*#' requirements.txt 2>/dev/null | sed 's/[><=!].*//' | tr -d ' ' | sort | cat - Dockerfile 2>/dev/null | md5sum | cut -d' ' -f1)
_PREV_HASH=$(cat "$_HASH_FILE" 2>/dev/null || echo "")
_NEED_BUILD=false
if [ "$_CUR_HASH" != "$_PREV_HASH" ]; then
  echo "[update] requirements/Dockerfile changed — will rebuild"
  _NEED_BUILD=true
fi

# ── 새 커밋 없고 재빌드도 불필요하면 스킵 ──────────────────────────────────────
if [ "$BEFORE" = "$AFTER" ] && [ "$_NEED_BUILD" = "false" ]; then
  echo "[update] no new commits, skipping docker restart"
  exit 0
fi

[ "$BEFORE" != "$AFTER" ] && echo "[update] new commits: $BEFORE → $AFTER"

# ── docker 재시작 ────────────────────────────────────────────────────────────
docker compose stop stock-bot stock-web 2>/dev/null || true
docker compose rm -f stock-bot stock-web 2>/dev/null || true

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER" 2>/dev/null || echo "")
if [ "$_NEED_BUILD" = "true" ] || echo "$CHANGED" | grep -qE '^(requirements\.txt|Dockerfile)'; then
  echo "[update] rebuilding image..."
  # 빌드 시작 전에 해시 저장 (cron 재진입 방지 — 빌드가 1분 이상 걸릴 수 있음)
  echo "$_CUR_HASH" > "$_HASH_FILE"
  docker compose up -d --build stock-bot stock-web
else
  echo "[update] code/config changed — restarting"
  docker compose up -d stock-bot stock-web
fi
