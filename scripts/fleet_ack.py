#!/usr/bin/env python3
"""
fleet_ack.py - ACK-VERIFIED dispatch to a Claude Code tmux session.

Closes the command-protocol gap: "I pasted it" -> "it confirmed it did it."

Flow:
  1. Resolve the session's registrar entry (short name, config_dir, cwd, uuid) and
     its transcript jsonl path from the fleet registry.
  2. Build a unique dispatch MARKER and a completion TOKEN, and write both into the
     payload - the marker as its own line, the token as the reply instruction.
  3. Arm a DELIVERY ACK with the daemon BEFORE dispatching, then dispatch via
     fleet_dispatch.sh (ready-gated, bracketed paste, first-line verify).
  4. Arm a COMPLETION WATCH with the daemon for the token, and return immediately.
  5. The daemon owns both deadlines: it satisfies the ack when the marker lands as a
     real user turn (else ACK-MISSED = re-deliver), and satisfies the watch when the
     token appears as a model line (else SENTINEL-MISSED = check in / re-arm).

WHY THE DAEMON OWNS THIS (and why the old blocking poll is gone):
  Pane-visibility is NOT delivery proof - a busy auto-mode pane can swallow the paste
  before submit, which is exactly why fleet_dispatch.sh's "LANDED" is a weak signal.
  fleet_watch.process_acks exists to catch precisely that (2026-09-10), and
  process_watches catches the never-finished case, each with its own deadline. The
  previous implementation ignored both and instead BLOCKED, polling the transcript for
  up to ack_timeout seconds. That duplicated the daemon's detection, cost minutes of
  wall-clock per dispatch, and reported a bogus SENT-NO-ACK whenever a session was
  legitimately still working past the timeout.

Exit: 0 = dispatched + deadlines armed, 1 = not dispatched (not ready / absent).
"""

import argparse, glob, json, os, re, subprocess, sys, time

HERE = os.path.expanduser('~/.hermes/scripts/cc-watch')

# fleet_transcript owns transcript location (uuid glob first, live process second).
# Import from this file's real directory so the symlinked cc-watch copy works too.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_transcript

# A payload that opens with a SENTINEL CONTRACT names its own done token, and that is the
# token the session will actually emit. The wrapper token this script invents is a SECOND
# token the session was never told to prefer, so a completion watch armed on it can only
# false-fire SENTINEL-MISSED while the real token sits unused in the transcript.
CONTRACT_CUE = re.compile(r'(exact done token|done token|completion token|done criteria?|emit exactly|emits? exactly|repl(?:y|ies) (?:instead )?with exactly)', re.I)
TOKEN_RE = re.compile(r'\b(DONE-[A-Za-z0-9._:-]+|FW[0-9A-Fa-f]{2,}-[A-Za-z0-9._-]+)\b')
# The second, stricter shape a contract takes: the token ALONE on its own line, optionally
# behind a short label ("DONE:", "EXACT DONE TOKEN:"). Cue vocabulary drifts between authors
# -- overwatch writes "DONE CRITERION:" and then the bare token, or "DONE: DONE-..." -- and a
# missed cue is exactly what armed a watch on the WRONG token and produced the false
# SENTINEL-MISSED this rule exists to prevent. A token mentioned mid-sentence still does not
# count (that is the passing reference, not a contract).
OWN_LINE = re.compile(r'^\s*(?:[A-Z][A-Z \-]{0,24}:)?\s*'
                      r'(DONE-[A-Za-z0-9._:-]+|FW[0-9A-Fa-f]{2,}-[A-Za-z0-9._-]+)\s*$')
# A contract whose token is shaped differently ("DONE TOKEN: TERRATAURI-RESUME-DONE") was
# silently downgraded to the wrapper token: the payload named one token, the daemon watched
# another, and the completion showed up as a false SENTINEL-MISSED with the real token left
# unused in the transcript. Observed live 2026-09-27. A cue line is authoritative, so when no
# strict token is found on it, take the uppercased-hyphenated word and SAY SO, rather than
# quietly arming a different token. Mid-sentence references stay excluded: only the cue line
# and the two lines under it are probed.
LOOSE_TOKEN_RE = re.compile(r'^\s*(?:[A-Z][A-Z \-]{0,24}:)?\s*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s*$')
# A payload can carry a cue while its token sits further down than the two-line probe window --
# seen live 2026-09-30: a contract whose "DONE CRITERION:" was followed by three numbered asks
# and only then the token. The rule returned None, the watch armed on the wrapper token, and the
# pasted payload told the session to emit TWO different tokens: whichever it picked, the daemon
# was watching the other one. Nothing is guessed here -- a whole-body scan would happily adopt a
# token mentioned in passing -- the mismatch is REPORTED so the caller fixes the phrasing.
STRAY_TOKEN_RE = re.compile(r'\b(DONE-[A-Za-z0-9._:-]+|FW[0-9A-Fa-f]{2,}-[A-Za-z0-9._-]+)\b')
WRAPPER_SHAPE = re.compile(r'DONE-fw[0-9a-f]{8}-[A-Za-z0-9._-]+')
REG = os.path.join(HERE, 'fleet_registry.json')
SLUG = os.path.join(HERE, 'fleet_watch.slug')
DISPATCH = os.path.expanduser('~/Developer/hermes-fleet-manager/scripts/fleet_dispatch.sh')
WATCH = os.path.join(HERE, 'fleet_watch.py')

# How long a dispatch marker may take to appear as a real USER TURN before we call the
# delivery swallowed. Measured from the daemon log over 99 confirmed deliveries
# (2026-09-21): min 0.2s, median 3.6s, p90 8.2s, worst ever 23.2s. This was a flat 2.0
# minutes -- five times the worst case ever observed -- which left a swallowed dispatch
# sitting for minutes instead of seconds. Default is ~2.5x the worst observed case.
ACK_DEADLINE_S = float(os.environ.get("FLEET_ACK_DEADLINE_S", "60"))
ACK_DEADLINE_MIN = ACK_DEADLINE_S / 60.0   # callers that still measure in minutes

# A payload that carries a sentinel contract is a milestone-sized unit by construction — that is
# what a done-token is for — so a caller-supplied deadline under this can only produce the false
# SENTINEL-MISSED the 1800s default exists to stop. Observed live: a 181s deadline armed by a
# caller false-fired while the session was 3 minutes into the work and still going.
MIN_CONTRACT_DEADLINE_S = 300

# Transcript record types that mean the session RECEIVED the paste, even though it is not
# (yet) a `message.role == "user"` turn. A busy session ATTACHES the paste to its queue, and
# the queue drains into a user turn later. Without this, every dispatch to a working session
# reads as undelivered.
#
# `queue-operation` alone is deliberately NOT a receipt. It is queue bookkeeping, and a paste
# parked behind Claude Code's "ctrl+x ctrl+s to send now" prompt writes exactly one of those
# with no attachment. Counting it as delivery made a parked paste report LANDED while the
# payload sat unsent in the composer and the session stayed idle at a bare prompt.
RECEIPT_TYPES = frozenset({"attachment"})

# What a parked paste leaves behind: queue bookkeeping, never attached, never a turn. Telling
# this apart from an absent paste matters, because an absent paste should be re-sent and a
# parked one must NOT be, or the retry stacks a second copy on top of the first.
PARKED_TYPE = "queue-operation"


def completion_deadline(ack_timeout, ctok, floor=MIN_CONTRACT_DEADLINE_S):
    """The deadline to arm, given the caller's request and whether the payload names a contract."""
    if ctok and ack_timeout < floor:
        return floor
    return ack_timeout


def delivered(tmux, marker, path=None):
    """True if `marker` was RECEIVED by this session, per its transcript.

    The pane only proves the text is on screen; an unsubmitted draft is indistinguishable
    there. The transcript is the truth -- the text lands in it only once the session
    actually received it. Returns None when the transcript cannot be read (unknown, not
    a lie), True/False otherwise.

    A BUSY session takes the paste as a QUEUED turn: the transcript records it as a
    `queue-operation` / `attachment` record rather than a `message.role == "user"` line,
    and only becomes a user turn when the queue drains. Both are receipt. Requiring `user`
    made three dispatches on 2026-09-26 report NOT-SUBMITTED while the daemon logged
    ACK-OK seconds later -- the message was in hand, just not yet a turn. Insert queued
    records count; anything else in the transcript does not.
    """
    if path is None:
        _short, path = resolve(tmux)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, errors="replace") as fh:
            for ln in fh:
                if marker not in ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                role = (obj.get("message") or {}).get("role") or obj.get("type")
                if role == "user":
                    return True
                if role in RECEIPT_TYPES:
                    return True
    except Exception:
        return None
    return False


def parked(tmux, marker, path=None):
    """True if `marker` reached this session only as queue bookkeeping, never as an attachment.

    This is the send-now trap. A multi-line paste to a BUSY session is held in the composer
    behind Claude Code's "ctrl+x ctrl+s to send now" prompt rather than being submitted. The
    transcript gains a `queue-operation` line for it and nothing else, so the text is on
    screen, in the bookkeeping, and not in the session's hands until the key is pressed.

    The caller needs this apart from `delivered()`: a genuinely absent paste should be
    re-pasted, while a parked one must not be, because re-pasting stacks a second copy on the
    first. Returns None when the transcript cannot be read (unknown, not a lie).
    """
    if path is None:
        _short, path = resolve(tmux)
    if not path or not os.path.exists(path):
        return None
    seen_parked = False
    try:
        with open(path, errors="replace") as fh:
            for ln in fh:
                if marker not in ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                role = (obj.get("message") or {}).get("role") or obj.get("type")
                if role == "user" or role in RECEIPT_TYPES:
                    return False          # it did arrive, so it is not parked
                if role == PARKED_TYPE:
                    seen_parked = True
    except Exception:
        return None
    return seen_parked


def contract_token(text):
    """The done token a sentinel-contract payload names for the session to emit, or None.

    Only a cue line counts ('Exact done token ...', 'emit EXACTLY ...'), and only tokens on that
    line or the two lines under it -- the contract states its token right next to the cue, and a
    looser scan would grab a token mentioned in passing inside the task text. Returns the LAST
    candidate, because a contract that lists a NEEDS-INPUT alternative states the done token
    second. None means the payload has no contract of its own: the caller falls back to the
    wrapper token, and nothing is guessed.

    Falls back to OWN_LINE (the token alone on its own line) when no cue vocabulary matched, so a
    contract phrased in words this file has not seen yet is still read instead of being silently
    downgraded to the wrapper token. A cue line whose token is neither of the two known shapes
    falls back to LOOSE_TOKEN_RE and warns the caller, because arming a different token from the
    one the payload names is what produces the false SENTINEL-MISSED.
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not CONTRACT_CUE.search(ln):
            continue
        for probe in lines[i:i + 3]:
            m = TOKEN_RE.search(probe)
            if m:
                return m.group(1)
        for probe in lines[i:i + 3]:
            m = LOOSE_TOKEN_RE.match(probe)
            if m:
                sys.stderr.write(
                    f"WARNING: contract token {m.group(1)!r} is not the DONE-/FW- shape; using it "
                    f"as the done token anyway. Prefer 'DONE TOKEN: DONE-<slug>' so the watch and "
                    f"the payload cannot disagree.\n")
                return m.group(1)
    found = None
    for ln in lines:
        m = OWN_LINE.match(ln)
        if m:
            found = m.group(1)
    return found


ALT_TOKEN_RE = re.compile(r'\bNEEDS-INPUT-[A-Za-z0-9_.:-]+\b')


def alt_token(text, ctok=None):
    """The alternative 'ended blocked' token a contract names beside its done token, or None.

    A contract that can legitimately END BLOCKED names a NEEDS-INPUT-<same slug> token so the
    session can say 'I could not proceed because X' instead of going silent. The daemon could
    only ever satisfy a watch on the done token, so a session that took the alternative route --
    the route the payload explicitly offered -- still fired SENTINEL-MISSED at the deadline and
    bought the operator a pointless check-in. Live, 2026-09-30: a session ended blocked on a
    human input, emitted the NEEDS-INPUT token its contract named, and the watch expired anyway.

    Only tokens on a cue line or inside its two-line probe window count, the same strictness as
    contract_token: a NEEDS-INPUT token mentioned in passing inside the task text is not an
    alternative this payload is offering. None means the payload names none.
    """
    lines = text.splitlines()
    found = None
    for i, ln in enumerate(lines):
        if not CONTRACT_CUE.search(ln):
            continue
        for probe in lines[i:i + 3]:
            m = ALT_TOKEN_RE.search(probe)
            if m and m.group(0) != ctok:
                found = m.group(0)
    return found


def unadopted_token_warning(body, ctok):
    """A warning when the payload names a done token the contract rule did not adopt, else None.

    The cue can be present while the token sits deeper than the probe window, so the rule returns
    None and the wrapper token is armed: the payload then asks the session for two tokens, and the
    watch only fires if it happens to pick the wrapper's. Report it rather than guess.
    """
    if ctok or not CONTRACT_CUE.search(body):
        return None
    named = [t for t in STRAY_TOKEN_RE.findall(body) if not WRAPPER_SHAPE.fullmatch(t)]
    if not named:
        return None
    return (f'WARNING: payload names {named[-1]!r} but the contract-token rule did not adopt it '
            f'(a token counts on the cue line, on the two lines under it, or alone on its own '
            f'line). The watch is armed on the wrapper token, so the payload now asks for two '
            f'tokens; write it as "DONE TOKEN: DONE-<slug>".')


def blocked_token(token, wrapper_token):
    """The NEEDS-INPUT route offered beside the done token, derived from it.

    Same slug, short name and timestamp, so the two tokens read as one contract rather than two.
    Falls back to the wrapper token when the payload's own contract token is not DONE-shaped,
    because a watch can only be satisfied by a token that shares its shape.
    """
    base = token if token.startswith('DONE-') else wrapper_token
    return base.replace('DONE-', 'NEEDS-INPUT-', 1)


def wrap_payload(body, marker, token, ctok, atok=None):
    """The dispatched payload: the operator's text, the delivery marker, the token instruction.

    Pure so the contract-token rule can be tested without dispatching. The marker has to be IN
    the payload: the daemon proves delivery by finding it in a real USER turn, so a marker passed
    out-of-band could never be satisfied. When the payload carries its own contract token, that
    token is the ONE the session is told to emit -- naming a second, wrapper token is how a
    session ends up emitting the contract token while the daemon watches the other one.

    ``atok`` names the route for ending BLOCKED on a person, and every dispatch now offers one
    whether or not the operator's text did. Without it a session that legitimately stops for a
    human has no token to emit, goes silent, and its watch expires into a LANE 2 (silent)
    SENTINEL-MISSED -- so the operator never learns the work is parked. Live 2026-10-01: a
    dispatched brief ended in four decisions addressed to the operator, the session said so in
    prose, and nothing reached her for two and a half hours.
    """
    if ctok:
        instr = (f"When you have actually DONE what this asks, emit EXACTLY this one line and "
                 f"nothing else: {token}\n(the token above is your contract's done token — "
                 f"do not emit any other completion token.)\n")
    else:
        instr = (f"When you have actually DONE what this asks, reply with EXACTLY this token "
                 f"and nothing else: {token}\n")
    if atok:
        instr += (f"If you cannot proceed without a person, reply instead with EXACTLY this one "
                  f"line and nothing else: {atok}\n")
    return f"{body}\n\n[{marker}]\n{instr}"


def read_slug():
    if os.path.exists(SLUG):
        s = open(SLUG).read().strip()
        if re.fullmatch(r'fw[0-9a-f]{8}', s):
            return s
    s = 'fw' + os.urandom(4).hex()
    with open(SLUG, 'w') as f:
        f.write(s)
    return s


def resolve(tmux):
    d = json.load(open(REG))
    for e in d.get('sessions', []):
        if e.get('name') == tmux:
            trans, _source = fleet_transcript.resolve(e, tmux=tmux)
            return e.get('short') or tmux, trans
    return tmux, None


def watch(sub, tmux, value, deadline_min, note='', alt=''):
    """Arm a daemon deadline (file-based control plane; safe while the daemon runs).

    sub='ack' -> watch for the dispatch MARKER landing as a real user turn (delivery).
    sub='add' -> watch for the completion TOKEN appearing as a model line.
    alt        -> a second token that also SATISFIES an 'add' watch, for contracts that name a
                  NEEDS-INPUT alternative for ending blocked (see alt_token). Without it the
                  session can take the route the payload offered and still false-fire.
    Returns the watch id, or None if arming failed (reported, never silently ignored).
    """
    flag = '--marker' if sub == 'ack' else '--token'
    cmd = [sys.executable, WATCH, 'watch', sub, '--session', tmux,
           flag, value, '--deadline-min', f'{deadline_min:.3f}', '--note', note]
    if alt:
        cmd += ['--alt-token', alt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'  watch {sub} FAILED: {e}')
        return None
    out = (r.stdout or r.stderr).strip().splitlines()
    out = out[0] if out else ''
    print(f'  {out[:170]}')
    m = re.search(r'id=([0-9a-f]+)', out)
    return m.group(1) if m and 'ARMED' in out else None


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "delivered":
        if len(argv) < 3:
            print("usage: fleet_ack.py delivered <session> <marker>")
            return 2
        r = delivered(argv[1], argv[2])
        print("DELIVERED" if r else ("UNKNOWN" if r is None else "NOT-DELIVERED"))
        # sys.exit, not return: the entry point discards main()'s return value, so a
        # `return 1` here still exits 0 and callers see success for a failed check.
        sys.exit(0 if r else (3 if r is None else 1))
    if argv and argv[0] == "parked":
        # Inspection seam for the send-now trap. Exit 0 = parked (the caller must commit it,
        # never re-paste), 1 = not parked (a real absence, safe to re-paste), 3 = unreadable.
        if len(argv) < 3:
            print("usage: fleet_ack.py parked <session> <marker>")
            return 2
        r = parked(argv[1], argv[2])
        print("PARKED" if r else ("UNKNOWN" if r is None else "NOT-PARKED"))
        sys.exit(0 if r else (3 if r is None else 1))
    if argv and argv[0] == "contract-token":
        # Inspection seam: show what the completion watch WOULD be armed on, without
        # dispatching. Lets the contract-token rule be checked against a real payload.
        if len(argv) < 2:
            print("usage: fleet_ack.py contract-token <payload>")
            return 2
        try:
            body = open(argv[1], errors="replace").read()
        except OSError as e:
            print(f"UNREADABLE: {e}")
            sys.exit(1)
        tok = contract_token(body)
        print(tok if tok else "NONE")
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument('tmux'); ap.add_argument('payload')
    ap.add_argument('ack_timeout', nargs='?', type=int, default=1800,
                    help='seconds for the session to FINISH (the completion watch). Default 30 '
                         'min: a milestone-sized unit does not complete in the 3 min this used '
                         'to default to, and a deadline that short only ever false-fires '
                         'SENTINEL-MISSED.')
    a = ap.parse_args()
    if not os.path.isfile(a.payload):
        print('EMPTY-FILE'); sys.exit(1)

    slug = read_slug()
    short, trans = resolve(a.tmux)
    ms = int(time.time() * 1000)
    wrapper_token = f'DONE-{slug}-{short}-{ms}'
    marker = f'DISPATCH-{short}-{ms}'
    body = open(a.payload, errors='replace').read()
    ctok = contract_token(body)
    atok = alt_token(body, ctok)
    warn = unadopted_token_warning(body, ctok)
    if warn:
        print('  ' + warn)
    armed = completion_deadline(a.ack_timeout, ctok)
    if armed != a.ack_timeout:
        print(f'  completion deadline {a.ack_timeout}s raised to {armed}s: the payload carries a '
              f'sentinel contract, and a short deadline on one only false-fires SENTINEL-MISSED.')
        a.ack_timeout = armed
    elif a.ack_timeout < MIN_CONTRACT_DEADLINE_S:
        print(f'  WARNING: completion deadline {a.ack_timeout}s is under '
              f'{MIN_CONTRACT_DEADLINE_S}s — that is the known false-MISSED generator for a real '
              f'unit of work.')
    # One token, and it is the contract's own: the session is told to emit exactly one thing,
    # so exactly one thing can satisfy the watch. Arming the wrapper token alongside a contract
    # that names its own is what made every dispatch false-fire at its deadline.
    token = ctok or wrapper_token
    # Every dispatch offers a blocked route, whether or not the payload named one. A task that can
    # legitimately stop for a person needs a token to say so; without it the session goes silent,
    # the watch expires, and SENTINEL-MISSED is LANE 2 (silent), so the park is never reported.
    if not atok:
        atok = blocked_token(token, wrapper_token)

    tmp = a.payload + f'.ack.{token[11:22]}'
    with open(tmp, 'w') as w:
        w.write(wrap_payload(body, marker, token, ctok, atok))

    # Arm delivery BEFORE dispatching - arming after could miss a fast user-turn write.
    ack_id = watch('ack', a.tmux, marker, ACK_DEADLINE_MIN,
                   note=f'dispatch {os.path.basename(a.payload)}')

    r = subprocess.run([DISPATCH, a.tmux, tmp, str(120)],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    os.unlink(tmp)
    if 'LANDED' not in out and 'UNCERTAIN' not in out:
        if ack_id:
            subprocess.run([sys.executable, WATCH, 'watch', 'ack-cancel', '--id', ack_id],
                           capture_output=True)
        # The dispatcher distinguishes a real miss from "cannot tell": NOT-LANDED /
        # NOT-SUBMITTED mean the transcript was readable and the marker is absent, while
        # NOT-READY / NO-SESSION / EMPTY-FILE mean it never went out at all. Name which.
        reason = 'NOT-DELIVERED' if re.search(r'NOT-LANDED|NOT-SUBMITTED', out) else 'NOT-READY'
        print(f'{reason} ({r.returncode}): {out.splitlines()[-1] if out else "no info"}')
        sys.exit(1)

    # Completion deadline. No blocking poll: the daemon fires either the satisfied signal
    # or SENTINEL-MISSED, and a miss wakes the agent instead of faking a result here.
    watch('add', a.tmux, token, a.ack_timeout / 60.0,
          note=f'completion for {os.path.basename(a.payload)}', alt=atok or '')
    _altline = (f'\nWATCH-ALT {atok} also satisfies this watch: it is offered for ending blocked on a '
                f'person, so taking that route answers the contract instead of expiring it.'
                if atok else '')
    print(f'WATCH-ARMED token={token} completion_deadline={a.ack_timeout}s '
          f'ack_deadline={ACK_DEADLINE_MIN}min - daemon reports satisfied or MISSED{_altline}')
    sys.exit(0)


if __name__ == '__main__':
    main()
