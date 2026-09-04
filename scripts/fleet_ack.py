#!/usr/bin/env python3
"""
fleet_ack.py - ACK-VERIFIED dispatch to a Claude Code tmux session.

Closes the command-protocol gap: "I pasted it" -> "it confirmed it did it."

Flow:
  1. Resolve the session's registrar entry (short name, config_dir, cwd, uuid) and
     its transcript jsonl path from the fleet registry.
  2. Generate a unique ACK token `DONE-<slug>-<short>-<unix_ms>` using the daemon's
     current slug (~/.hermes/scripts/cc-watch/fleet_watch.slug, fw+8hex).
  3. Append an instruction to the payload telling the session to reply with that
     exact token when it has DONE what the payload asks.
  4. Send via fleet_dispatch.sh (ready-gated, bracketed paste, first-line verify).
  5. Poll the transcript (incremental, from last file offset) for the ACTION token.
  6. Report CONFIRMED (token seen) vs SENT-NO-ACK (sent but no confirmation).

Exit: 0 = CONFIRMED, 2 = SENT-NO-ACK, 1 = not dispatched (not ready / absent).
"""
import argparse, json, os, re, subprocess, sys, time

HERE = os.path.expanduser('~/.hermes/scripts/cc-watch')
REG = os.path.join(HERE, 'fleet_registry.json')
SLUG = os.path.join(HERE, 'fleet_watch.slug')
DISPATCH = os.path.expanduser('~/Developer/hermes-fleet-manager/scripts/fleet_dispatch.sh')

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tmux'); ap.add_argument('payload')
    ap.add_argument('ack_timeout', nargs='?', type=int, default=180)
    a = ap.parse_args()
    if not os.path.isfile(a.payload):
        print('EMPTY-FILE'); sys.exit(1)

    slug = read_slug()
    short, trans = resolve(a.tmux)
    token = f'DONE-{slug}-{short}-{int(time.time()*1000)}'

    tmp = a.payload + f'.ack.{token[11:22]}'
    with open(a.payload) as f, open(tmp, 'w') as w:
        w.write(f.read())
        w.write(f'\n\nWhen you have actually DONE what this asks, reply with EXACTLY this token and nothing else: {token}\n')

    r = subprocess.run([DISPATCH, a.tmux, tmp, str(120)],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    os.unlink(tmp)
    if 'LANDED' not in out and 'UNCERTAIN' not in out:
        print(f'NOT-READY ({r.returncode}): {out.splitlines()[-1] if out else "no info"}')
        sys.exit(1)

    # poll the transcript incrementally for the token
    if not trans or not os.path.exists(trans):
        print(f'SENT-NO-ACK token={token} trans={trans}')  # cannot verify
        sys.exit(2)
    offset = os.path.getsize(trans)
    deadline = time.time() + a.ack_timeout
    waited = 0
    while time.time() < deadline:
        time.sleep(3); waited += 3
        try:
            with open(trans, 'rb') as f:
                f.seek(offset); new = f.read().decode('utf-8', 'replace'); offset = f.tell()
        except OSError:
            continue
        if token in new:
            print(f'CONFIRMED token={token} waited={waited}s')
            sys.exit(0)
    print(f'SENT-NO-ACK token={token} waited={waited}s (timeout {a.ack_timeout}s)')
    sys.exit(2)

if __name__ == '__main__':
    main()
