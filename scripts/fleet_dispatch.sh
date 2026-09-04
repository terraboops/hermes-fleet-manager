#!/usr/bin/env bash
#
# fleet_dispatch.sh - reliably inject a file's contents into a Claude Code tmux pane.
#
# Root cause this fixes: dispatches were TRUNCATED because we pasted on fixed
# sleeps after an Escape/C-c, racing the pane's settle -> the FRONT of the paste
# got eaten/merged with the interrupt or a busy line editor. Correct discipline
# (matches the tmux+Claude deep-dive's wait_for_claude_idle pattern):
#   1. POLL the pane until it is at a CLEAN prompt: only COMPLETED turns ("· done")
#      and no LIVE spinner/thinking token in the recent live region (last ~8 lines).
#      A turn logged as "done" is historical, not current activity.
#   2. If a blocking feedback menu ("How is Claude doing this session?") is up,
#      dismiss it, then keep polling.
#   3. If still busy past a grace period and --interrupt was given, land on a prompt
#      with ONE C-c, then re-poll. Default is to WAIT for the agent to finish
#      naturally. Never blind-interrupt a session that is legitimately working.
#   4. Paste with BRACKETED PASTE (paste-buffer -p) so Claude treats it as one paste
#      instead of keystrokes a busy edge-case can eat.
#   5. Send Enter, then VERIFY the first line of the payload is visible in the pane.
#
# Usage: fleet_dispatch.sh <session> <file> [timeout_seconds] [--interrupt]
# Exit: 0 = dispatched + first line confirmed landed; 1 = timeout / not ready;
#       2 = dispatched but could not confirm the first line.
set -uo pipefail
S="${1:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
FILE="${2:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
TIMEOUT="${3:-120}"
INTERRUPT=0; [ "${4:-}" = "--interrupt" ] && INTERRUPT=1

if ! tmux has-session -t "$S" 2>/dev/null; then echo "NO-SESSION"; exit 1; fi
if [ ! -s "$FILE" ]; then echo "EMPTY-FILE"; exit 1; fi

BUF="fd_$$_$RANDOM"
log() { printf '[dispatch %s] %s\n' "$S" "$*" >&2; }

# busy() -> 0 if the LIVE region (last 8 lines, excluding COMPLETED/BANNER lines)
# shows a LIVE spinner/thinking token or the blocking feedback menu. Excluded as
# historical/display cruft: completed turns ("✻ <Verb> for <dur> · done" or bare
# "✻ <Verb> for <dur>"), the recurring "✻ Running scheduled task (...)" banner, the
# "Update installed · Restart to update" + "Auto-update failed" banners, and "· done".
busy() {
  local LAST
  LAST=$(tmux capture-pane -t "$S" -p -S -8 2>/dev/null \
    | grep -viE "· done|Running scheduled task|Restart to update|Update installed|Auto-update failed|claude doctor" \
    | grep -vE '[✽✳✢✻✷].*for [0-9]+ ?[sm]')
  [ -z "$LAST" ] && return 0
  echo "$LAST" | grep -qE \
    '✽|✳|✢|✻|✷|Thinking…|Thinking\.\.\.|Inferring|Concoct|Nebuliz|Hullabal|Skedaddl|Tinker|· [0-9]+ ?s|How is Claude doing'
  return $?
}

elapsed=0
gave_cc=0
while busy; do
  if [ "$elapsed" -ge "$TIMEOUT" ]; then echo "NOT-READY"; exit 1; fi
  # auto-dismiss the "How is Claude doing this session?" feedback menu so it can't trap us
  if tmux capture-pane -t "$S" -p -S -8 2>/dev/null | grep -q "How is Claude doing"; then
    tmux send-keys -t "$S" Escape; sleep 1; log "dismissed feedback menu"
  fi
  if [ "$INTERRUPT" -eq 1 ] && [ "$gave_cc" -eq 0 ] && [ "$elapsed" -ge 6 ]; then
    log "busy - sending one C-c to land on a prompt (--interrupt)"
    tmux send-keys -t "$S" C-c; gave_cc=1
  fi
  sleep 3; elapsed=$((elapsed+3))
done
log "clean prompt after ${elapsed}s"

tmux load-buffer -b "$BUF" "$FILE"
# -p brackets the paste so Claude's line editor cannot eat the leading chars
tmux paste-buffer -p -b "$BUF" -t "$S"
tmux send-keys -t "$S" Enter

MARK="$(head -1 "$FILE" | cut -c1-40)"
sleep 1
if tmux capture-pane -t "$S" -p -S -10 2>/dev/null | grep -Fq "$MARK"; then
  log "first line visible - landed"
  echo "LANDED"
  exit 0
else
  log "WARN first line NOT visible in pane (may be truncated)"
  echo "UNCERTAIN-${MARK}"
  exit 2
fi
