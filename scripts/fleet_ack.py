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

import argparse, json, os, re, subprocess, sys, time

HERE = os.path.expanduser('~/.hermes/scripts/cc-watch')
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


def delivered(tmux, marker):
    """True if `marker` is a real USER TURN in this session's transcript.

    The pane only proves the text is on screen; an unsubmitted draft is indistinguishable
    there. The transcript is the truth -- the text lands in it only once the session
    actually received it. Returns None when the transcript cannot be read (unknown, not
    a lie), True/False otherwise.
    """
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
    except Exception:
        return None
    return False


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
            cwd = os.path.expanduser(e.get('cwd') or '')
            enc = '-' + cwd.lstrip('/').replace('/', '-')
            cfg = os.path.expanduser(e.get('config_dir') or '')
            uuid = e.get('uuid')
            trans = os.path.join(cfg, 'projects', enc, f'{uuid}.jsonl')
            return e.get('short') or tmux, trans
    return tmux, None


def watch(sub, tmux, value, deadline_min, note=''):
    """Arm a daemon deadline (file-based control plane; safe while the daemon runs).

    sub='ack' -> watch for the dispatch MARKER landing as a real user turn (delivery).
    sub='add' -> watch for the completion TOKEN appearing as a model line.
    Returns the watch id, or None if arming failed (reported, never silently ignored).
    """
    flag = '--marker' if sub == 'ack' else '--token'
    try:
        r = subprocess.run([sys.executable, WATCH, 'watch', sub, '--session', tmux,
                            flag, value, '--deadline-min', f'{deadline_min:.3f}',
                            '--note', note],
                           capture_output=True, text=True, timeout=60)
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
    ap = argparse.ArgumentParser()
    ap.add_argument('tmux'); ap.add_argument('payload')
    ap.add_argument('ack_timeout', nargs='?', type=int, default=180,
                    help='deadline in seconds for the session to FINISH (the completion watch)')
    a = ap.parse_args()
    if not os.path.isfile(a.payload):
        print('EMPTY-FILE'); sys.exit(1)

    slug = read_slug()
    short, trans = resolve(a.tmux)
    ms = int(time.time() * 1000)
    token = f'DONE-{slug}-{short}-{ms}'
    marker = f'DISPATCH-{short}-{ms}'

    tmp = a.payload + f'.ack.{token[11:22]}'
    with open(a.payload) as f, open(tmp, 'w') as w:
        w.write(f.read())
        # The marker has to be IN the payload: the daemon proves delivery by finding it
        # in a real USER turn, so a marker passed out-of-band could never be satisfied.
        w.write(f'\n\n[{marker}]\n')
        w.write(f'When you have actually DONE what this asks, reply with EXACTLY this token and nothing else: {token}\n')

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
        print(f'NOT-READY ({r.returncode}): {out.splitlines()[-1] if out else "no info"}')
        sys.exit(1)

    # Completion deadline. No blocking poll: the daemon fires either the satisfied signal
    # or SENTINEL-MISSED, and a miss wakes the agent instead of faking a result here.
    watch('add', a.tmux, token, a.ack_timeout / 60.0,
          note=f'completion for {os.path.basename(a.payload)}')
    print(f'WATCH-ARMED token={token} completion_deadline={a.ack_timeout}s '
          f'ack_deadline={ACK_DEADLINE_MIN}min - daemon reports satisfied or MISSED')
    sys.exit(0)


if __name__ == '__main__':
    main()
