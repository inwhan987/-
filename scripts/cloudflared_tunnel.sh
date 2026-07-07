#!/usr/bin/env bash
# cloudflared quick tunnel 러너 — 파이(LAN-only) 인바운드를 무료 터널로 노출하고
# 현재 공개 URL 을 data/tunnel_url.txt 에 기록한다. 웹앱(_ci_callback_base)이 그 파일을
# 읽어 CI 디스패치 때 콜백 URL 로 실어보낸다. quick tunnel 은 재시작마다 URL 이 바뀌므로
# 이 스크립트가 매 기동 시 파일을 갱신한다(systemd Restart=always 와 함께 쓰면 자동 복구).
#
# 사용:  scripts/cloudflared_tunnel.sh            # 기본 localhost:8001 노출
#        LOCAL_PORT=8001 scripts/cloudflared_tunnel.sh
#
# 사전: cloudflared 설치(/usr/local/bin/cloudflared). 계정·도메인 불필요(trycloudflare).
set -euo pipefail

LOCAL_PORT="${LOCAL_PORT:-8001}"
# 이 스크립트는 <repo>/scripts/ 에 있으므로 데이터 파일은 <repo>/data/ (컨테이너 /app/data 마운트).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${TUNNEL_DATA_DIR:-$SCRIPT_DIR/../data}"
mkdir -p "$DATA_DIR"
URL_FILE="$DATA_DIR/tunnel_url.txt"

echo "[tunnel] cloudflared → http://localhost:${LOCAL_PORT}, url 기록: ${URL_FILE}"

# cloudflared 출력을 그대로 통과시키면서 trycloudflare URL 을 잡아 파일에 쓴다.
# (한 번 뜬 뒤에도 라인이 또 나오면 갱신 — 무해)
cloudflared tunnel --no-autoupdate --url "http://localhost:${LOCAL_PORT}" 2>&1 | \
while IFS= read -r line; do
  echo "$line"
  url="$(printf '%s' "$line" | grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | head -n1 || true)"
  if [ -n "$url" ]; then
    printf '%s' "$url" > "$URL_FILE"
    echo "[tunnel] 현재 URL 기록됨: $url"
  fi
done
