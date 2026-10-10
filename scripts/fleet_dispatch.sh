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
#   4. NEVER send keys into a choice UI (menu). Report MENU-BLOCKED and stop.
#   5. Anything already in the composer is SAVED, then CLEARED, then RESTORED and the
#      restore is VERIFIED - see below.
#   6. Paste with BRACKETED PASTE (paste-buffer -p) so Claude treats it as one paste
#      instead of keystrokes a busy edge-case can eat.
#   7. Send Enter, then CONFIRM THE RECEIPT FROM THE TRANSCRIPT - never from the pane.
#
# COMPOSER DRAFT IS BORROWED, NOT DESTROYED (rewritten 2026-09-27, Terra):
#   "it should save whatever is in the input, clear it, send the message, verify sent,
#    restore it, and verify restore."
#   The composer may hold a HUMAN's unsubmitted instruction, which a naive clear
#   destroys silently. So the sequence is now:
#     save   -> fleet_input.py reads the composer's text (between the box borders, so
#               a wrapped multi-line draft is captured whole) into a temp file, and
#               also appends it to the stash log as a backstop
#     clear  -> fleet_input.py --clear (refuses a menu, refuses an empty box)
#     send   -> the payload, receipt-verified from the transcript
#     restore-> the saved text is pasted back WITHOUT Enter, so it is a draft again
#     verify -> the composer is re-read and the text compared, whitespace-normalised;
#               a mismatch reports DRAFT-RESTORE-FAILED and names the stash
#   A draft is never restored over our own unsent payload (that would concatenate), and
#   a choice UI is never cleared at all - the run stops with MENU-BLOCKED.
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
#       5 = MENU-BLOCKED   -> a choice UI is up; no keys were sent
set -uo pipefail
S="${1:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
FILE="${2:?usage: fleet_dispatch.sh <session> <file> [timeout] [--interrupt]}"
TIMEOUT="${3:-120}"
INTERRUPT=0; [ "${4:-}" = "--interrupt" ] && INTERRUPT=1
log() { printf '[dispatch %s] %s\n' "$S" "$*" >&2; }

CCW="$HOME/.hermes/scripts/cc-watch"
ACKPY="$CCW/fleet_ack.py"
TSCRIPT="$CCW/fleet_transcript.py"
INPUT="$CCW/fleet_input.py"
STASH="${FLEET_DRAFT_STASH:-$HOME/.hermes/logs/fleet-drafts.log}"

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
  # The pointer has to ask for the WORK, not just the read. A pointer that says only "read this
  # then reply with <tok>" gets exactly that: the session reads the file, emits the token and
  # stops, and the brief inside is never acted on. Verified 2026-10-10 on a 6.6 KB brief: the
  # session acked in 9 seconds and did nothing.
  printf 'Read %s and carry out what it asks. When you have actually DONE it, reply with EXACTLY %s and nothing else.\n' "$FILE" "$TOK" > "$POINTER"
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

# ---- COMPOSER: read it, decide, save + clear (never touch a menu) ----
# The composer is the ONLY place an unsent draft exists, so it is read through
# fleet_input.py, which takes the bordered box around the cursor rather than grepping
# for a prompt character (a numbered choice renders with the same marker).
DRAFT_FILE="$(mktemp -t fdd_XXXXX)"
DINFO="$(python3 "$INPUT" "$S" --save-draft "$DRAFT_FILE" 2>/dev/null || true)"
case "$DINFO" in
  *'"menu": true'*)
    log "BLOCKED: a choice UI is up in the composer; sending no keys into it"
    echo "MENU-BLOCKED"
    exit 5 ;;
esac
DRAFT_SAVED=0
DRAFT_TEXT=""
# GHOST TEXT IS NOT A DRAFT (2026-09-27). Claude Code renders a dim placeholder hint in the
# composer, and the reader has always reported it as `ghost`/`dim`. Treating it as a draft made
# the dispatcher stash a hint and hand it back to the operator as "your unsubmitted draft" -
# twice, on two different sessions, and both times it was Claude's own grey suggestion.
GHOST=0
case "$DINFO" in
  *'"ghost": true'*|*'"dim": true'*) GHOST=1 ;;
esac
if [ "$GHOST" = "1" ]; then
  log "composer shows ghost/placeholder text (not a draft) - clearing the hint, nothing to save or restore"
  python3 "$INPUT" "$S" --clear >/dev/null 2>&1
  sleep 1
elif [ -s "$DRAFT_FILE" ]; then
  DRAFT_SAVED=1
  DRAFT_TEXT="$(cat "$DRAFT_FILE")"
  mkdir -p "$(dirname "$STASH")" 2>/dev/null
  printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$S" "$DRAFT_TEXT" >> "$STASH" 2>/dev/null
  log "composer holds a real draft - saved (stash: $STASH) and clearing: $DRAFT_TEXT"
  echo "DRAFT-STASHED: $DRAFT_TEXT"
  python3 "$INPUT" "$S" --clear >/dev/null 2>&1
  sleep 1
fi

# norm_file: collapse whitespace so a wrapped composer reads the same as the saved copy
norm_file() { tr -s '[:space:]' ' ' < "$1" | sed -E 's/^ //; s/ $//'; }

# restore_draft: put the saved text BACK into the composer as a draft (no Enter), then
# verify it landed. Called on every exit path that follows a clear, so the operator's
# text is never the price of a dispatch.
restore_draft() {
  [ "$DRAFT_SAVED" = "1" ] || return 0
  local CUR_FILE RESTORE_FILE AFTER_FILE DBUF CINFO
  CUR_FILE=$(mktemp -t fdc_XXXXX)
  CINFO=$(python3 "$INPUT" "$S" --save-draft "$CUR_FILE" 2>/dev/null || true)
  # A ghost/placeholder hint is not an occupant: it must not block the restore.
  case "$CINFO" in
    *'"ghost": true'*|*'"dim": true'*) : > "$CUR_FILE" ;;
  esac
  if [ -s "$CUR_FILE" ]; then
    # The composer is NOT empty. Either our own payload is still sitting there unsent,
    # or the session/operator has typed something new since we cleared. Either way
    # appending would concatenate two messages, so leave it and say so - the draft is
    # in the stash. (Reading the composer, not the pane: a delivered marker is echoed
    # in the pane's transcript render, which made a pane-tail check misfire and skip
    # every restore.)
    if [ "$(norm_file "$CUR_FILE")" = "$(norm_file "$DRAFT_FILE")" ]; then
      log "composer already holds the saved draft"
      echo "DRAFT-RESTORED"
    else
      log "NOT restoring the draft: the composer holds other text (preserved in $STASH)"
      echo "DRAFT-NOT-RESTORED-COMPOSER-OCCUPIED"
    fi
    return 0
  fi
  RESTORE_FILE=$(mktemp -t fdr_XXXXX)
  printf '%s' "$DRAFT_TEXT" > "$RESTORE_FILE"
  DBUF="fdd_$$_$RANDOM"
  tmux load-buffer -b "$DBUF" "$RESTORE_FILE"
  tmux paste-buffer -p -b "$DBUF" -t "=$S:"     # deliberately NO Enter: it is a draft
  sleep 1
  AFTER_FILE=$(mktemp -t frv_XXXXX)
  python3 "$INPUT" "$S" --save-draft "$AFTER_FILE" >/dev/null 2>&1
  if [ -s "$AFTER_FILE" ] && [ "$(norm_file "$RESTORE_FILE")" = "$(norm_file "$AFTER_FILE")" ]; then
    log "composer draft restored and verified"
    echo "DRAFT-RESTORED"
  else
    log "could NOT verify the restored draft; the text is preserved in $STASH"
    echo "DRAFT-RESTORE-FAILED"
  fi
}

paste_once() {
  tmux load-buffer -b "$BUF" "$PASTEFILE"
  # -p brackets the paste so Claude's line editor cannot eat the leading chars
  tmux paste-buffer -p -b "$BUF" -t "=$S:"
  tmux send-keys -t "=$S:" Enter
  # A LONG paste parks in the composer behind "ctrl+x ctrl+s to send now" instead of
  # submitting on Enter. Both existing checks then read as delivered while nothing was
  # sent: the first line IS visible in the pane, and the transcript DOES gain a
  # queue-operation record. Only the record TYPE separates a parked paste from a
  # delivered one, and a parked paste never becomes a turn. Send the key when the
  # prompt is on screen. Verified 2026-10-09: a payload sat unsent for minutes, the
  # verdict read LANDED, and the session stayed idle at a bare prompt.
  sleep 1
  if tmux capture-pane -pt "=$S:" -S -40 2>/dev/null | grep -qi "ctrl+x ctrl+s to send"; then
    tmux send-keys -t "=$S:" C-x C-s
    log "paste parked behind the send-now prompt; sent C-x C-s"
  fi
}

# receipt() -> exits with fleet_ack.py's verdict: 0 delivered, 1 absent, 3 unknown.
receipt() { python3 "$ACKPY" delivered "$S" "$1" >/dev/null 2>&1; }

# parked_check() -> 0 when the marker reached the session only as queue bookkeeping, which is
# the send-now trap rather than a miss. Never re-paste on a 0.
parked_check() { python3 "$ACKPY" parked "$S" "$1" >/dev/null 2>&1; }

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
  restore_draft
  echo "LANDED"
  exit 0
fi

if [ "$VERDICT" = "UNCERTAIN" ]; then
  # We could not read a transcript, so we do not know - and a blind re-send is exactly
  # how a delivered message gets posted twice. Report UNKNOWN and stop.
  log "cannot tell: no readable transcript for this session (source=$TSOURCE). Reporting UNCERTAIN and NOT re-sending."
  restore_draft
  echo "UNCERTAIN-NO-TRANSCRIPT-${MARK}"
  exit 3
fi

# Before re-pasting, rule out the send-now trap. A parked paste is in the transcript only as
# queue bookkeeping, so the receipt check calls it absent - and re-pasting would stack a
# second copy on top of the first. Commit the parked copy instead of sending another.
if parked_check "$MARK"; then
  log "the paste is parked behind the send-now prompt, not absent; committing it instead of re-pasting"
  tmux send-keys -t "=$S:" C-x C-s
  sleep 1
  VERDICT="$(wait_for_receipt)"
  if [ "$VERDICT" = "LANDED" ]; then
    log "delivery confirmed after committing the parked paste: $MARK is in the transcript"
    restore_draft
    echo "LANDED-PARKED-COMMITTED"
    exit 0
  fi
  if [ "$VERDICT" = "UNCERTAIN" ]; then
    log "cannot tell after committing the parked paste: transcript unreadable. Reporting UNCERTAIN; do not re-send."
    restore_draft
    echo "UNCERTAIN-NO-TRANSCRIPT-${MARK}"
    exit 3
  fi
  log "the parked paste did not clear after C-x C-s; reporting it rather than re-pasting a duplicate"
  restore_draft
  echo "PARKED-NOT-COMMITTED-${MARK}"
  exit 5
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
  restore_draft
  echo "LANDED-RETRY"
  exit 0
fi
if [ "$VERDICT" = "UNCERTAIN" ]; then
  log "cannot tell after the retry: the transcript became unreadable. Reporting UNCERTAIN; do not re-send."
  restore_draft
  echo "UNCERTAIN-NO-TRANSCRIPT-${MARK}"
  exit 3
fi
if tmux capture-pane -t "=$S:" -p -S -10 2>/dev/null | grep -Fq -- "$MARK"; then
  log "text is in the pane but never became a turn (unsubmitted draft, or the Enter was swallowed)"
  restore_draft
  echo "NOT-SUBMITTED-AFTER-RETRY-${MARK}"
  exit 2
fi
log "the marker is in neither the transcript nor the pane: the paste did not land"
restore_draft
echo "NOT-LANDED-AFTER-RETRY-${MARK}"
exit 4
