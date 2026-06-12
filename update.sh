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
  # ── self-heal: rebase 실패 = diverge 굳음 → origin/main 으로 강제 정렬 ──────
  # 파이 working tree에 코드 파일이 modified로 남으면("Entry not uptodate" 등)
  # rebase가 막혀 1 vs N diverge가 영구화된다. 이때 origin이 코드의 정본이므로
  # 강제 reset 으로 자가복구한다. 미push 커밋(자정 data 백업 등)은 _selfheal_bak
  # 브랜치 + reflog 에 남아 사후 복구가 가능하다.
  echo "[update] rebase 실패 — self-heal로 origin/main 강제 정렬"
  git rebase --abort 2>/dev/null || true
  git branch -f _selfheal_bak HEAD 2>/dev/null || true   # 미push 커밋 안전망
  # backup.py가 건 skip-worktree 때문에 reset이 막히지 않도록 해제
  git update-index --no-skip-worktree "$_OVR" 2>/dev/null || true
  if ! git reset --hard origin/main; then
    echo "[update] self-heal reset 실패, 종료"
    _sync_overrides
    exit 1
  fi
  _sync_overrides   # .env.overrides 를 origin(정본) 값으로 복원
  echo "[update] self-heal 완료 → origin/main 정렬 (직전 상태: _selfheal_bak)"
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

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER" 2>/dev/null || echo "")

# ── data/ 백업 커밋뿐이면 재시작 생략 ──────────────────────────────────────────
# 자정 백업(trades/reviews/news 등 data/ 만 커밋)이 매일 봇을 재시작시키는 문제 방지.
# 코드·설정 파일이 하나라도 섞여 있으면 정상 재시작 경로로 진행.
if [ "$_NEED_BUILD" = "false" ] && [ -n "$CHANGED" ] \
   && ! echo "$CHANGED" | grep -qv '^data/'; then
  echo "[update] data/ backup commits only — skipping docker restart"
  exit 0
fi

# ── docker 재시작 ────────────────────────────────────────────────────────────
docker compose stop stock-bot stock-web leader-bot 2>/dev/null || true
docker compose rm -f stock-bot stock-web leader-bot 2>/dev/null || true
if [ "$_NEED_BUILD" = "true" ] || echo "$CHANGED" | grep -qE '^(requirements\.txt|Dockerfile)'; then
  echo "[update] rebuilding image..."
  # 빌드 시작 전에 해시 저장 (cron 재진입 방지 — 빌드가 1분 이상 걸릴 수 있음)
  echo "$_CUR_HASH" > "$_HASH_FILE"
  docker compose up -d --build stock-bot stock-web leader-bot
else
  echo "[update] code/config changed — restarting"
  docker compose up -d stock-bot stock-web leader-bot
fi
