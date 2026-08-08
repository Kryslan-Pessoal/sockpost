#!/usr/bin/env bash
#
# smoke.sh - end to end check with no test framework required.
#
# Starts a daemon on a throwaway socket and database, delivers a message to a
# listening channel, exercises the acknowledgement path, and shuts everything
# down. It never touches the default socket or database, so it is safe to run
# on a machine where sockpost is already in use.
#
#   tools/smoke.sh
#
# Exit status: 0 when every check passes, 1 otherwise.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/sockpost-smoke.XXXXXX")"
# Unix socket paths are limited to about 100 bytes, so this one cannot live in
# the work directory. mktemp -u picks an unpredictable name in a shared place.
SOCKET="$(mktemp -u /tmp/sockpost-smoke.XXXXXX).sock"

export SOCKPOST_SOCKET="$SOCKET"
export SOCKPOST_DB="$WORKDIR/queue.db"
export SOCKPOST_PID="$WORKDIR/daemon.pid"
export SOCKPOST_LOG="$WORKDIR/daemon.log"
export SOCKPOST_AUTOSTART=0
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

DAEMON_PID=""
LISTENER_PID=""
FAILURES=0

sockpost() { "$PYTHON" -m sockpost "$@"; }

cleanup() {
  [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null
  [ -n "$DAEMON_PID" ] && kill "$DAEMON_PID" 2>/dev/null
  sleep 0.3
  rm -f "$SOCKET"
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

pass() { printf 'ok    %s\n' "$1"; }

fail() {
  printf 'FAIL  %s\n' "$1"
  [ $# -gt 1 ] && printf '      got: %s\n' "$2"
  FAILURES=$((FAILURES + 1))
}

expect_contains() {
  # expect_contains <label> <haystack> <needle>
  case "$2" in
    *"$3"*) pass "$1" ;;
    *) fail "$1" "$2" ;;
  esac
}

wait_for() {
  # wait_for <file> <needle> [seconds]
  local file="$1" needle="$2" limit="${3:-10}" waited=0
  while [ "$waited" -lt "$((limit * 10))" ]; do
    if [ -f "$file" ] && grep -q -- "$needle" "$file"; then
      return 0
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  return 1
}

echo "sockpost smoke test"
echo "  socket:   $SOCKET"
echo "  database: $SOCKPOST_DB"
echo

# 1. daemon ------------------------------------------------------------------
sockpost daemon > "$WORKDIR/daemon.out" 2>&1 &
DAEMON_PID=$!
disown "$DAEMON_PID" 2>/dev/null || true
if wait_for "$WORKDIR/daemon.out" "daemon=up" 10; then
  pass "daemon starts"
else
  fail "daemon starts" "$(cat "$WORKDIR/daemon.out" 2>/dev/null)"
  echo; echo "smoke test FAILED"; exit 1
fi

# 2. listener ----------------------------------------------------------------
sockpost listen --id worker > "$WORKDIR/worker.out" 2>&1 &
LISTENER_PID=$!
disown "$LISTENER_PID" 2>/dev/null || true
if wait_for "$WORKDIR/worker.out" "connected id=worker" 10; then
  pass "listener connects"
else
  fail "listener connects" "$(cat "$WORKDIR/worker.out" 2>/dev/null)"
fi

# 3. delivery ----------------------------------------------------------------
OUT="$(sockpost send --from planner --to worker --text 'build the thing' 2>&1)"
expect_contains "send returns a message id" "$OUT" "queued id=1"

if wait_for "$WORKDIR/worker.out" "message id=1" 10; then
  pass "message reaches the listener"
  expect_contains "message body is intact" "$(cat "$WORKDIR/worker.out")" \
    'text="build the thing"'
else
  fail "message reaches the listener" "$(cat "$WORKDIR/worker.out" 2>/dev/null)"
fi

# 4. automatic acknowledgement ----------------------------------------------
sleep 0.5
OUT="$(sockpost unread 2>&1)"
expect_contains "queue drains after acknowledgement" "$OUT" "unread n=0"

# 5. durability and manual acknowledgement ----------------------------------
OUT="$(sockpost send --from planner --to offline-agent --text 'wait for me' 2>&1)"
expect_contains "message for an offline channel is queued" "$OUT" "queued id=2"

OUT="$(sockpost unread 2>&1)"
expect_contains "queued message is listed as unread" "$OUT" "to=offline-agent"

OUT="$(sockpost ack 2 --from intruder 2>&1)"
expect_contains "a third party cannot acknowledge" "$OUT" "not the recipient"

OUT="$(sockpost ack 2 --from offline-agent 2>&1)"
expect_contains "the recipient can acknowledge" "$OUT" "ack id=2"

OUT="$(sockpost unread 2>&1)"
expect_contains "queue is empty again" "$OUT" "unread n=0"

# 6. status ------------------------------------------------------------------
OUT="$(sockpost status 2>&1)"
expect_contains "status reports the daemon" "$OUT" "daemon=up"
expect_contains "status reports the channel" "$OUT" "id=worker"
expect_contains "status pings the channel" "$OUT" "pong id=worker"

# 7. shutdown ----------------------------------------------------------------
OUT="$(sockpost stop 2>&1)"
expect_contains "daemon stops on request" "$OUT" "daemon=stopped"
DAEMON_PID=""

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "smoke test PASSED"
  exit 0
fi
echo "smoke test FAILED ($FAILURES checks)"
exit 1
