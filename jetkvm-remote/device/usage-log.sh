#!/bin/sh
# eth0 traffic logger for JetKVM (busybox sh).
#   usage-log.sh          run the logger loop (started from start.sh)
#   usage-log.sh report   print daily and monthly totals

DIR=${USAGE_DIR:-/userdata/tailscale}
STATS=${USAGE_STATS:-/sys/class/net/eth0/statistics}
UPTIME=${USAGE_UPTIME:-/proc/uptime}
STATE=$DIR/usage.state
LOG=$DIR/usage.log
PIDFILE=/tmp/usage-log.pid
INTERVAL=600

sample() {
  rx=$(cat "$STATS/rx_bytes")
  tx=$(cat "$STATS/tx_bytes")
  up=$(cut -d' ' -f1 "$UPTIME")
  # The clock reads 2021 until NTP syncs after boot; wait instead of logging under a wrong date.
  [ "$(date +%Y)" -lt 2025 ] && return
  if [ -f "$STATE" ]; then
    read -r prx ptx pup < "$STATE"
    awk -v rx="$rx" -v tx="$tx" -v up="$up" -v prx="$prx" -v ptx="$ptx" -v pup="$pup" \
        -v ts="$(date '+%Y-%m-%d %H:%M')" '
      function delta(cur, prev) {
        if (up + 0 < pup + 0) return cur
        if (cur + 0 < prev + 0) return cur + 4294967296 - prev
        return cur - prev
      }
      BEGIN { printf "%s %.0f %.0f\n", ts, delta(rx, prx), delta(tx, ptx) }' >> "$LOG"
  fi
  echo "$rx $tx $up" > "$STATE"
}

report() {
  if [ ! -s "$LOG" ]; then
    echo "아직 기록이 없습니다 (10분마다 기록)."
    return
  fi
  echo "날짜          받음(MB)   보냄(MB)"
  awk '{ r[$1] += $3; t[$1] += $4 }
       END { for (k in r) printf "%s %10.1f %10.1f\n", k, r[k] / 1048576, t[k] / 1048576 }' "$LOG" | sort
  echo
  echo "월            받음(GB)   보냄(GB)"
  awk '{ m = substr($1, 1, 7); r[m] += $3; t[m] += $4 }
       END { for (k in r) printf "%s    %10.2f %10.2f\n", k, r[k] / 1073741824, t[k] / 1073741824 }' "$LOG" | sort
}

if [ "$1" = "report" ]; then
  report
  exit 0
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "usage-log already running (pid $(cat "$PIDFILE"))"
  exit 0
fi
echo $$ > "$PIDFILE"

while true; do
  sample
  sleep "$INTERVAL"
done
