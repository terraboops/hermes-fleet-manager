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
log() { printf '[dispatch %s] %s\n' "$S" "$*" >&2; }

if ! tmux has-session -t "$S" 2>/dev/null; then echo "NO-SESSION"; exit 1; fi
if [ ! -s "$FILE" ]; then echo "EMPTY-FILE"; exit 1; fi

# ---- LENGTH GUARD (2026-09-09) ----
# A large payload is NEVER pasted as a raw block — even at a clean prompt, bracketed
# paste can still interleave with a long edit in a busy pane and get its front/middle
# eaten (observed: a multi-line directive landed as "seems cut off ??", losing the body).
# For payloads over the thresholds, keep the full text on disk at $FILE and send only a
# ONE-LINE pointer the agent reads via cat — nothing enters the tmux input except a short,
# un-breakable line. Thresholds are tuned to catch real directive blocks.
MAXLINES="160"; MAXBYTES="6144"
LINES=$(wc -l < "$FILE"); BYTES=$(wc -c < "$FILE")
if [ "$LINES" -gt "$MAXLINES" ] || [ "$BYTES" -gt "$MAXBYTES" ]; then
  TOK="${DISPATCH_TOKEN:-DONE-read-$(date +%s)}"
  POINTER=$(mktemp -t fdp_XXXXX)
  printf 'Read %s then reply with %s\n' "$FILE" "$TOK" > "$POINTER"
  log "large payload (${LINES}L / ${BYTES}B) -> one-line pointer ${TOK}"
  FILE="$POINTER"
else
  log "payload ${LINES}L / ${BYTES}B within thresholds -> direct bracketed paste"
fi

BUF="fd_$$_$RANDOM"

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

# stale_draft() -> 0 if the composer's input line holds a non-empty, unsubmitted draft
# (text after the "❯" prompt marker). A trapped draft is why an agent idles thinking it
# already answered, and why raw Enter is sometimes swallowed in auto-mode. Guard + verify.
stale_draft() {
  tmux capture-pane -t "$S" -p -S -6 2>/dev/null | grep -qE '^\s*❯\s+\S'
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

# STALE-DRAFT GUARD (2026-09-09): clear any unsubmitted text sitting in the composer BEFORE
# paste — otherwise it doubles into the payload, and the agent can idle on a half-sent answer.
# One C-c resets Claude Code's line editor (observed: raw Enter was swallowed in auto-mode).
if stale_draft; then
  log "clearing stale composer draft"
  tmux send-keys -t "$S" C-c; sleep 2
fi

tmux load-buffer -b "$BUF" "$FILE"
# -p brackets the paste so Claude's line editor cannot eat the leading chars
tmux paste-buffer -p -b "$BUF" -t "$S"
tmux send-keys -t "$S" Enter

MARK="$(head -1 "$FILE" | cut -c1-40)"
# SUBMIT VERIFY: real proof of delivery = the payload's first line becomes visible in the pane
# AFTER paste+Enter. Do NOT gate LANDED on a composer-drain heuristic (2026-09-09 REGRESSION:
# a false stale-draft match made every verify fail, and the retry loop re-sent the message up to
# 4x into wolfgang + members. Reverted to the original single-shot check.)
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
