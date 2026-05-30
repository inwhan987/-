#!/bin/bash
# auto-update: git pull --rebase + docker rebuild on source change
set -e

# ── 동시 실행 방지 (빌드가 1분 이상 걸릴 때 cron 중복 실행 차단) ────────────────
exec 9>/tmp/update_stock.lock
flock -n 9 || { echo "[update] already running, skipping"; exit 0; }

cd /home/inwhan/stock-bot

# ── .env.overrides: origin/main 이 진실의 원천 ──────────────────────────────
# PC(또는 Claude)가 git에 push한 값이 정본. 과거에는 파일을 mv 후 rebase 했는데,
# autostash 가 "추적파일 삭제"를 stash→재적용하면서 .env.overrides 가 사라지고
# stash 가 무한 누적되는 버그가 있었음(매분 크론에서 매번 파일 증발).
# → mv 방식 폐기. fetch 후 origin 버전으로 작업트리를 동기화해 rebase 충돌을 없애고,
#   rebase 뒤 다시 origin 버전을 강제 적용(자가복구)한다.
_OVR=".env.overrides"

# 이전 버전의 잔여 백업 정리
[ -f "${_OVR}.rebase_bak" ] && rm -f "${_OVR}.rebase_bak"

# origin 버전을 .env.overrides 에 "원자적"으로 반영하는 헬퍼.
#   - `>` 직접 리다이렉트는 파일을 0바이트로 truncate 후 다시 채우므로, 그 찰나에
#     봇의 _reload_env_if_changed 가 빈 파일을 읽어 .env 기본값으로 핫리로드됐다가
#     1초 뒤 원복되는 노이즈가 발생했음(매분 크론에서 가끔 race).
#   - temp 파일에 쓰고 mv(같은 FS=원자적) → 봇이 절대 partial/빈 파일을 못 봄.
#   - 내용이 동일하면 아예 건드리지 않아 mtime 변화도, 불필요한 reload 도 없음.
_sync_overrides() {
  local tmp="${_OVR}.tmp.$$"
  if git cat-file -p origin/main:"$_OVR" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    if ! cmp -s "$tmp" "$_OVR"; then
      mv -f "$tmp" "$_OVR"
    else
      rm -f "$tmp"
    fi
  else
    rm -f "$tmp"   # origin에 파일이 없거나 빈 경우 기존 파일 보존
  fi
}

# ── git pull --rebase ────────────────────────────────────────────────────────
BEFORE=$(git rev-parse HEAD)

git fetch origin main

# rebase 전: .env.overrides 를 origin 버전으로 맞춰 stash/충돌 원천 차단
_sync_overrides
git add "$_OVR" 2>/dev/null || true

git rebase --autostash origin/main || {
  echo "[update] rebase 실패, abort 후 종료"
  git rebase --abort 2>/dev/null || true
  _sync_overrides
  exit 1
}

AFTER=$(git rev-parse HEAD)

# rebase 성공: origin 버전 강제 적용(자가복구) — 파일이 비었거나 사라져도 복원됨
_sync_overrides
echo "[update] .env.overrides origin 버전 동기화 완료"

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
