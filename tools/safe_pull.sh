#!/usr/bin/env bash
# 안전한 git pull — 로컬 전용 파일 자동 백업·복원
#
# 사용:
#   ./tools/safe_pull.sh
#
# 보호 대상 (rebase 중 사라질 수 있는 untracked 로컬 파일):
#   - data/backtest_history.json
set -e

cd "$(dirname "$0")/.."

PROTECTED=(
  "data/backtest_history.json"
)

# 백업
declare -A BACKUPS
for f in "${PROTECTED[@]}"; do
  if [ -f "$f" ]; then
    bak="${f}.bak.$$"
    cp -p "$f" "$bak"
    BACKUPS[$f]=$bak
    echo "  backed up: $f → $bak"
  fi
done

# pull
echo "→ git pull --rebase --autostash -X theirs ..."
git pull --rebase --autostash -X theirs

# 복원
for f in "${!BACKUPS[@]}"; do
  bak="${BACKUPS[$f]}"
  if [ -f "$bak" ]; then
    if [ ! -f "$f" ] || [ "$bak" -nt "$f" ]; then
      mv "$bak" "$f"
      echo "  restored: $f"
    else
      rm "$bak"
    fi
  fi
done

echo "✓ safe pull 완료"
