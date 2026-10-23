#!/bin/sh
# Tailscale watchdog for JetKVM (busybox sh).
# Restarts tailscaled when it dies, or when it stays offline for MAX_FAILS
# checks in a row while the LAN gateway is still reachable.

TS_DIR=/userdata/tailscale
SOCK=$TS_DIR/tailscaled.sock
LOG=$TS_DIR/watchdog.log
PIDFILE=/tmp/ts-watchdog.pid
INTERVAL=60
MAX_FAILS=3
FUNNEL_PORT=80

ts() { "$TS_DIR/tailscale" --socket="$SOCK" "$@"; }

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
  if [ "$(wc -c < "$LOG")" -gt 262144 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

# busybox `timeout` syntax differs between versions, so roll our own.
run_with_timeout() {
  secs=$1; shift
  "$@" > /tmp/ts-watchdog.out 2>&1 &
  p=$!
  i=0
  while kill -0 "$p" 2>/dev/null; do
    if [ "$i" -ge "$secs" ]; then
      kill -9 "$p" 2>/dev/null
      return 124
    fi
    sleep 1
    i=$((i + 1))
  done
  wait "$p"
}

start_daemon() {
  # JetKVM's system CA store can't verify Let's Encrypt, which breaks Funnel cert renewal.
  [ -f "$TS_DIR/cacert.pem" ] && export SSL_CERT_FILE="$TS_DIR/cacert.pem"
  "$TS_DIR/tailscaled" --tun=userspace-networking \
    --state="$TS_DIR/tailscaled.state" --socket="$SOCK" \
    >> "$TS_DIR/tailscaled.log" 2>&1 &
  sleep 15
  if ! ts funnel status 2>/dev/null | grep -q "Funnel on"; then
    log "funnel was off, re-enabling on port $FUNNEL_PORT"
    ts funnel --bg "$FUNNEL_PORT" >/dev/null 2>&1
  fi
}

restart_daemon() {
  log "restarting tailscaled: $1"
  killall tailscaled 2>/dev/null
  sleep 3
  pidof tailscaled >/dev/null && killall -9 tailscaled 2>/dev/null
  start_daemon
}

healthy() {
  run_with_timeout 15 ts status --peers=false --json || return 1
  grep -q '"BackendState": "Running"' /tmp/ts-watchdog.out &&
    grep -q '"Online": true' /tmp/ts-watchdog.out
}

gateway_ok() {
  gw=$(ip route | awk '/^default/ {print $3; exit}')
  [ -n "$gw" ] && ping -c 1 -W 2 "$gw" >/dev/null 2>&1
}

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "ts-watchdog already running (pid $(cat "$PIDFILE"))"
  exit 0
fi
echo $$ > "$PIDFILE"
log "watchdog started"

# Give the boot-time tailscaled a chance to come up before judging it.
sleep 30

fails=0
while true; do
  if ! pidof tailscaled >/dev/null; then
    log "tailscaled not running, starting it"
    start_daemon
    fails=0
  elif healthy; then
    fails=0
  elif ! gateway_ok; then
    # Router/LAN is down; restarting tailscaled would not help.
    fails=0
  else
    fails=$((fails + 1))
    log "tailscaled unhealthy ($fails/$MAX_FAILS)"
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      restart_daemon "offline for $MAX_FAILS checks"
      fails=0
    fi
  fi
  sleep "$INTERVAL"
done
