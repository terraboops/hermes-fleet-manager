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
#   5. Send Enter, then CONFIRM THE RECEIPT FROM THE TRANSCRIPT - never from the pane.
#
# RECEIPT CHECK (rewritten 2026-09-27):
#   Location comes from fleet_transcript.py, which resolves the transcript by UUID
#   (globbing the projects tree), then by the LIVE PROCESS's `--resume <uuid>`, and
#   only then falls back to the newest file. It never rebuilds the project-directory
#   name from the cwd - that rule already changed once (dots were not translated) and
#   made one session's transcript unfindable, so every dispatch to it was reported
#   NOT-LANDED and re-sent while the marker sat in the file twice.
#
#   The receipt is confirmed by fleet_ack.py's `delivered`, whose exit code is the
#   verdict and is now READ AS ONE:
#     0 = the marker is in the transcript  -> LANDED
#     1 = the marker is absent, and we COULD read the transcript -> a real miss
#     3 = we could not read the transcript -> UNKNOWN, not a miss
#   Conflating 1 and 3 is what makes a delivered message get sent twice, so an
#   UNKNOWN verdict stops the run and reports UNCERTAIN instead of retrying.
#
#   A marker is INJECTED when the payload does not carry one, so the receipt check
#   can never fall back to "the first 40 chars of line 1" - a string that can repeat
#   in the session's own text and satisfy the check without anything being sent.
#
# Usage: fleet_dispatch.sh <session> <file> [timeout_seconds] [--interrupt]
# Exit: 0 = LANDED (or LANDED-RETRY)        -> the marker is in the transcript
#       1 = NOT-READY / NO-SESSION / EMPTY-FILE -> never dispatched
#       2 = NOT-SUBMITTED  -> in the pane, never became a turn (unsubmitted draft)
#       3 = UNCERTAIN      -> cannot tell (transcript unresolvable); do NOT re-send
#       4 = NOT-LANDED     -> definite miss, and the transcript was readable
set -uo pipefail
S="${1:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
FILE="${2:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
TIMEOUT="${3:-120}"
INTERRUPT=0; [ "${4:-}" = "--interrupt" ] && INTERRUPT=1
log() { printf '[dispatch %s] %s\n' "$S" "$*" >&2; }

CCW="$HOME/.hermes/scripts/cc-watch"
ACKPY="$CCW/fleet_ack.py"
TSCRIPT="$CCW/fleet_transcript.py"

if ! tmux has-session -t "=$S:" 2>/dev/null; then echo "NO-SESSION"; exit 1; fi
if [ ! -s "$FILE" ]; then echo "EMPTY-FILE"; exit 1; fi

# ---- WHERE IS THE TRANSCRIPT (asked once, up front, and reported) ----
RESOLVED="$(python3 "$TSCRIPT" resolve "$S" 2>/dev/null || true)"
TPATH="$(printf '%s' "$RESOLVED" | cut -f1)"
TSOURCE="$(printf '%s' "$RESOLVED" | cut -f2)"
TDRIFT="$(printf '%s' "$RESOLVED" | cut -f3)"
TSOURCE="${TSOURCE:-unknown}"
if [ -n "$TPATH" ]; then
  log "transcript via $TSOURCE: $TPATH"
  if [ "$TDRIFT" = "drift" ]; then
    log "NOTE: the registry uuid differs from the running process - the registry is stale; the live conversation is the one being checked"
  fi
else
  log "CAREFUL: no transcript resolvable (source=$TSOURCE) - a receipt check will read UNCERTAIN, never a false miss"
fi

# ---- MARKER: a unique string that must appear in the transcript ----
MARK="$(grep -oE 'DISPATCH-[A-Za-z0-9_.-]+-[0-9]{10,}' "$FILE" 2>/dev/null | head -1)"
INJECTED=0
if [ -z "$MARK" ]; then
  SHORT="$(python3 - "$S" <<'PY' 2>/dev/null || true
import json, os, sys
try:
    d = json.load(open(os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')))
except Exception:
    sys.exit(0)
for e in d.get('sessions', []):
    if e.get('name') == sys.argv[1]:
        print(e.get('short') or sys.argv[1]); break
PY
)"
  SHORT="${SHORT:-$(printf '%s' "$S" | tr -c 'A-Za-z0-9_.-' '-')}"
  MARK="DISPATCH-${SHORT}-$(python3 -c 'import time;print(int(time.time()*1000))')"
  INJECTED=1
  log "payload carries no dispatch id - injecting $MARK so the receipt check has a unique marker"
fi

# ---- LENGTH GUARD (2026-09-09) ----
# A large payload is NEVER pasted as a raw block - even at a clean prompt, bracketed
# paste can still interleave with a long edit in a busy pane and get its front/middle
# eaten (observed: a multi-line directive landed as "seems cut off ??", losing the body).
# For payloads over the thresholds, keep the full text on disk at $FILE and send only a
# ONE-LINE pointer the agent reads via cat - nothing enters the tmux input except a short,
# un-breakable line.
MAXLINES="160"; MAXBYTES="6144"
LINES=$(wc -l < "$FILE"); BYTES=$(wc -c < "$FILE")
PASTEFILE="$FILE"
if [ "$LINES" -gt "$MAXLINES" ] || [ "$BYTES" -gt "$MAXBYTES" ]; then
  TOK="${DISPATCH_TOKEN:-DONE-read-$(date +%s)}"
  POINTER=$(mktemp -t fdp_XXXXX)
  printf 'Read %s then reply with %s\n' "$FILE" "$TOK" > "$POINTER"
  [ "$INJECTED" = "1" ] && printf '[%s]\n' "$MARK" >> "$POINTER"
  log "large payload (${LINES}L / ${BYTES}B) -> one-line pointer ${TOK}"
  PASTEFILE="$POINTER"
elif [ "$INJECTED" = "1" ]; then
  # Paste a copy carrying the marker; the caller's file is left untouched.
  PASTEFILE=$(mktemp -t fdp_XXXXX)
  cat "$FILE" > "$PASTEFILE"
  printf '\n[%s]\n' "$MARK" >> "$PASTEFILE"
  log "payload ${LINES}L / ${BYTES}B within thresholds -> direct bracketed paste"
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
  LAST=$(tmux capture-pane -t "=$S:" -p -S -8 2>/dev/null \
    | grep -viE "· done|Running scheduled task|Restart to update|Update installed|Auto-update failed|claude doctor|auto mode on|← for agents|Claude resuming /loop|/loop wakeup|no-op tick" \
    | grep -vE '[✽✳✢✻✷].*for [0-9]+ ?[sm]')
  [ -z "$LAST" ] && return 0
  echo "$LAST" | grep -qE \
    '✽|✳|✢|✻|✷|Thinking…|Thinking\.\.\.|Inferring|Concoct|Nebuliz|Hullabal|Skedaddl|Tinker|· [0-9]+ ?s\b|How is Claude doing'
  return $?
}

# stale_draft() -> 0 if the composer's input line holds a non-empty, unsubmitted draft
# (text after the "❯" prompt marker). A trapped draft is why an agent idles thinking it
# already answered, and why raw Enter is sometimes swallowed in auto-mode. Guard + verify.
stale_draft() {
  tmux capture-pane -t "=$S:" -p -S -6 2>/dev/null | grep -qE '^\s*❯\s+\S'
}

elapsed=0
gave_cc=0
while busy; do
  if [ "$elapsed" -ge "$TIMEOUT" ]; then echo "NOT-READY"; exit 1; fi
  # auto-dismiss the "How is Claude doing this session?" feedback menu so it can't trap us.
  # 2026-09-19: Escape alone can leave the menu up (observed trapping a dispatch until
  # NOT-READY) - if it is still there after Escape, press the menu's Dismiss key "0".
  if tmux capture-pane -t "=$S:" -p -S -8 2>/dev/null | grep -q "How is Claude doing"; then
    tmux send-keys -t "=$S:" Escape; sleep 1
    if tmux capture-pane -t "=$S:" -p -S -8 2>/dev/null | grep -q "How is Claude doing"; then
      tmux send-keys -t "=$S:" "0"; sleep 1; log "dismissed feedback menu (Escape+0)"
    else
      log "dismissed feedback menu"
    fi
  fi
  if [ "$INTERRUPT" -eq 1 ] && [ "$gave_cc" -eq 0 ] && [ "$elapsed" -ge 6 ]; then
    log "busy - sending one C-c to land on a prompt (--interrupt)"
    tmux send-keys -t "=$S:" C-c; gave_cc=1
  fi
  sleep 3; elapsed=$((elapsed+3))
done
log "clean prompt after ${elapsed}s"

# STALE-DRAFT GUARD (2026-09-09): clear any unsubmitted text sitting in the composer BEFORE
# paste - otherwise it doubles into the payload, and the agent can idle on a half-sent answer.
# One C-c resets Claude Code's line editor (observed: raw Enter was swallowed in auto-mode).
# 2026-09-26: a draft can be a HUMAN's in-progress steering message, not stale paste debris, and
# C-c destroys it silently. Stash it before clearing and announce it on stdout so the caller can
# surface it instead of the message vanishing.
if stale_draft; then
  DRAFT=$(tmux capture-pane -t "=$S:" -p -S -6 2>/dev/null \
    | grep -E '^[[:space:]]*❯[[:space:]]+[^[:space:]]' | sed -E 's/^[[:space:]]*❯[[:space:]]*//' | tail -1)
  if [ -n "$DRAFT" ]; then
    STASH="${FLEET_DRAFT_STASH:-$HOME/.hermes/logs/fleet-drafts.log}"
    mkdir -p "$(dirname "$STASH")" 2>/dev/null
    printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$S" "$DRAFT" >> "$STASH" 2>/dev/null
    log "clearing stale composer draft (stashed to $STASH): $DRAFT"
    echo "DRAFT-STASHED: $DRAFT"
  else
    log "clearing stale composer draft"
  fi
  tmux send-keys -t "=$S:" C-c; sleep 2
fi

paste_once() {
  tmux load-buffer -b "$BUF" "$PASTEFILE"
  # -p brackets the paste so Claude's line editor cannot eat the leading chars
  tmux paste-buffer -p -b "$BUF" -t "=$S:"
  tmux send-keys -t "=$S:" Enter
}

# receipt() -> exits with fleet_ack.py's verdict: 0 delivered, 1 absent, 3 unknown.
receipt() { python3 "$ACKPY" delivered "$S" "$1" >/dev/null 2>&1; }

ACK="${FLEET_DELIVERY_TIMEOUT_S:-60}"
# 99 confirmed deliveries: median 3.6s, p90 8.2s, worst 23.2s. 60s is ~2.5x the worst case.
wait_for_receipt() {   # echoes LANDED / ABSENT / UNCERTAIN
  local deadline rc
  deadline=$(( $(date +%s) + ACK ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    rc=0; receipt "$MARK" || rc=$?
    case "$rc" in
      0) echo "LANDED"; return 0 ;;
      3) echo "UNCERTAIN"; return 0 ;;
    esac
    sleep 2
  done
  echo "ABSENT"; return 0
}

paste_once
VERDICT="$(wait_for_receipt)"

if [ "$VERDICT" = "LANDED" ]; then
  log "delivery confirmed: $MARK is in the transcript"
  echo "LANDED"
  exit 0
fi

if [ "$VERDICT" = "UNCERTAIN" ]; then
  # We could not read a transcript, so we do not know - and a blind re-send is exactly
  # how a delivered message gets posted twice. Report UNKNOWN and stop.
  log "cannot tell: no readable transcript for this session (source=$TSOURCE). Reporting UNCERTAIN and NOT re-sending."
  echo "UNCERTAIN-NO-TRANSCRIPT-${MARK}"
  exit 3
fi

# ABSENT: we COULD read the transcript and $MARK is not in it. That is a real miss, so
# retry ONCE - safe precisely because the receipt check found nothing. (The pane is never
# the authority here: a delivered message can scroll out of its 10-line tail, which made a
# good dispatch report NOT-SUBMITTED four times in one sweep on 2026-09-26.)
log "not confirmed after ${ACK}s; retrying the paste once (the transcript was readable and the marker is absent)"
paste_once
VERDICT="$(wait_for_receipt)"

if [ "$VERDICT" = "LANDED" ]; then
  log "delivery confirmed on retry: $MARK is in the transcript"
  echo "LANDED-RETRY"
  exit 0
fi
if [ "$VERDICT" = "UNCERTAIN" ]; then
  log "cannot tell after the retry: the transcript became unreadable. Reporting UNCERTAIN; do not re-send."
  echo "UNCERTAIN-NO-TRANSCRIPT-${MARK}"
  exit 3
fi
if tmux capture-pane -t "=$S:" -p -S -10 2>/dev/null | grep -Fq "$MARK"; then
  log "text is in the pane but never became a turn (unsubmitted draft, or the Enter was swallowed)"
  echo "NOT-SUBMITTED-AFTER-RETRY-${MARK}"
  exit 2
fi
log "the marker is in neither the transcript nor the pane: the paste did not land"
echo "NOT-LANDED-AFTER-RETRY-${MARK}"
exit 4
