#!/bin/sh
# Tailscale watchdog for JetKVM (busybox sh).
# Restarts tailscaled when it dies, when it stays offline while the LAN
# gateway is reachable, or when the public Funnel URL stops answering even
# though tailscaled itself reports healthy.

TS_DIR=/userdata/tailscale
SOCK=$TS_DIR/tailscaled.sock
LOG=$TS_DIR/watchdog.log
PIDFILE=/tmp/ts-watchdog.pid
OUT=/tmp/ts-watchdog.out
INTERVAL=60
MAX_FAILS=3
FUNNEL_PORT=80
FUNNEL_CHECK_EVERY=2
FUNNEL_MAX_FAILS=3
FUNNEL_COOLDOWN=1800

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
  "$@" > "$OUT" 2>&1 &
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
  grep -q '"BackendState": "Running"' "$OUT" &&
    grep -q '"Online": true' "$OUT"
}

gateway_ok() {
  gw=$(ip route | awk '/^default/ {print $3; exit}')
  [ -n "$gw" ] && ping -c 1 -W 2 "$gw" >/dev/null 2>&1
}

# Fetches our own public Funnel URL. The OS resolver isn't MagicDNS in
# userspace-networking mode, so this goes out through the real Funnel
# ingress, the same path remote clients use.
funnel_ok() {
  run_with_timeout 15 ts funnel status || return 1
  url=$(grep -o 'https://[^ ]*\.ts\.net' "$OUT" | head -n 1)
  if [ -z "$url" ]; then
    log "funnel config missing, re-enabling on port $FUNNEL_PORT"
    ts funnel --bg "$FUNNEL_PORT" >/dev/null 2>&1
    return 0
  fi
  run_with_timeout 20 wget -q -O /dev/null "$url"
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
funnel_fails=0
loops=0
last_funnel_restart=0
while true; do
  loops=$((loops + 1))
  if ! pidof tailscaled >/dev/null; then
    log "tailscaled not running, starting it"
    start_daemon
    fails=0
    funnel_fails=0
  elif healthy; then
    fails=0
    if [ $((loops % FUNNEL_CHECK_EVERY)) -eq 0 ]; then
      if funnel_ok; then
        [ "$funnel_fails" -gt 0 ] && log "funnel reachable again"
        funnel_fails=0
      else
        funnel_fails=$((funnel_fails + 1))
        log "funnel unreachable ($funnel_fails/$FUNNEL_MAX_FAILS)"
        if [ "$funnel_fails" -ge "$FUNNEL_MAX_FAILS" ]; then
          now=$(date +%s)
          if [ $((now - last_funnel_restart)) -ge "$FUNNEL_COOLDOWN" ]; then
            restart_daemon "funnel unreachable for $FUNNEL_MAX_FAILS checks"
            last_funnel_restart=$now
          else
            log "funnel restart skipped (cooldown)"
          fi
          funnel_fails=0
        fi
      fi
    fi
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
