#!/bin/bash
# auto-update: git pull --rebase + docker rebuild on source change
set -e

cd /home/inwhan/stock-bot

# ── .env.overrides 값 보존 ────────────────────────────────────────────────────
# rebase 후 git HEAD 버전(PC가 추가한 새 키 포함)을 베이스로,
# 봇이 가진 기존 key=value 만 덮어씌움.
# → PC에서 새 키를 추가해도 봇 머신에 자동 반영됨 (새 키는 git 기본값 유지).
# → 봇 머신에서 .env.overrides 를 커밋하지 않으므로 diverge 방지.
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
  # 봇 버전 복원
  [ -f "$_OVR_BAK" ] && mv "$_OVR_BAK" "$_OVR"
  exit 1
}

AFTER=$(git rev-parse HEAD)

# ── .env.overrides: git HEAD 버전 기반 + 봇 값 적용 ──────────────────────────
if [ -f "$_OVR_BAK" ]; then
  python3 - "$_OVR" "$_OVR_BAK" <<'PYEOF'
import re, sys

ovr_path = sys.argv[1]
bak_path = sys.argv[2]

# 봇이 가진 key=value 파싱
saved = {}
with open(bak_path, encoding="utf-8") as f:
    for line in f:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            saved[k.strip()] = v.strip()

# git HEAD 버전 베이스로 봇 값 적용
try:
    with open(ovr_path, encoding="utf-8") as f:
        content = f.read()
    for k, v in saved.items():
        content = re.sub(rf"^{re.escape(k)}=.*", f"{k}={v}", content, flags=re.MULTILINE)
    with open(ovr_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[update] .env.overrides git 버전 베이스 + 봇 값 적용 완료 ({len(saved)}개 키)")
except FileNotFoundError:
    # git에 파일이 없으면 봇 버전 그대로 복원
    import shutil
    shutil.copy2(bak_path, ovr_path)
    print("[update] .env.overrides git 버전 없음 → 봇 버전 복원")
PYEOF
  rm -f "$_OVR_BAK"
fi

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
