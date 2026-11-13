#!/usr/bin/env python3
"""Manage the fleet-watch registry of Claude sessions (explicit, no auto-discovery).

The fleet-watch daemon tails ONLY sessions listed here. Register when you CREATE a
session; unregister when you CLOSE one. `check` verifies each registered session's
transcript exists — run it whenever the user asks for a status update, and re-register
any session whose transcript has gone stale (e.g. resumed to a new uuid).

Usage:
  fleet_reg.py list
  fleet_reg.py register <name> --short <s> --profile <p|w> --cwd <dir> [--uuid <id>]
  fleet_reg.py unregister <name>
  fleet_reg.py check          # prints per-session OK/MISSING + exit 1 if any missing
"""
import argparse, json, os, re, pathlib, sys, subprocess

def _cfg(key, default):
    p = os.environ.get("FLEET_CONFIG")
    if p:
        try:
            return os.path.expanduser(json.load(open(os.path.expanduser(p))).get(key) or default)
        except Exception:
            pass
    return os.path.expanduser(default)

REG = _cfg("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json")
# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
# Aliased so this block does not depend on where the file's own imports sit.
import os as _os, sys as _sys
import time
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import fleet_harness as _harness
_scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
CFG = _harness.config_dirs()   # profile name -> config dir, from config

def _provider_entry(entry, provider=None, codex_home=None):
    """Apply an explicit provider without mutating the shared profile config."""
    if provider == "codex":
        home = os.path.expanduser(codex_home or os.environ.get("CODEX_HOME") or "~/.codex")
        entry.update({"provider": "codex", "command": "codex", "config_dir": home,
                      "codex_home": home, "resume_flag": ["resume"]})
        entry.setdefault("codex_auto_trust_project", entry.get("profile") == "work")
    elif provider == "claude":
        entry["provider"] = "claude"
    return entry

def load():
    import fleet_registry_store
    return fleet_registry_store.load(REG)

def save(d):
    import fleet_registry_store
    fleet_registry_store.save(REG, d)

def promote(name, target, expected_source_uuid, transaction_dir):
    """Publish a verified canary under the stable fleet name, initially parked.

    Callers quiesce/park/close both panes first, then launch and verify the published
    UUID before marking up. Stable names keep paused watches and history attached.
    The manifest contains the exact source row for a scoped, reversible rollback.
    """
    import copy
    import shutil
    d = load()
    source = next((e for e in d['sessions'] if e.get('name') == name), None)
    if not source or source.get('uuid') != expected_source_uuid:
        raise ValueError('source identity changed')
    if source.get('profile') != 'work' or target.get('profile') != 'work':
        raise ValueError('promotion is work-profile only')
    if source.get('desired') != 'parked' or _probe_live(name) or _probe_live(target['name']):
        raise ValueError('park source and close both quiescent panes before promotion')
    provenance = target.get('source_provenance', {})
    source_provider = source.get('provider') or 'claude'
    target_provider = target.get('provider')
    if ((source_provider, target_provider) not in {('claude', 'codex'), ('codex', 'claude')}
            or provenance.get('session_id') != expected_source_uuid
            or (provenance.get('provider') and provenance['provider'] != source_provider)):
        raise ValueError('target provider/provenance mismatch')
    if not target.get('uuid') or not transcript_path(target):
        raise ValueError('target native transcript missing')
    source_cwd = os.path.expanduser(source['cwd'])
    if not pathlib.Path(source_cwd).is_dir():
        raise ValueError('source project directory missing')
    dest = pathlib.Path(transaction_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=False, mode=0o700)
    entry = copy.deepcopy(target)
    entry.update(name=name, short=source['short'], cwd=source_cwd, desired='parked')
    for key, filename in [('handoff_context', 'handoff-context.jsonl'), ('transcript', 'source-transcript.jsonl')]:
        original = pathlib.Path(provenance[key])
        shutil.copy2(original, dest / filename)
        os.chmod(dest / filename, 0o600)
        if key == 'handoff_context':
            entry['source_provenance']['original_handoff_context'] = str(original)
            entry['source_provenance'][key] = str(dest / filename)
    entry['migration_record'] = str(dest / 'promotion.json')
    record = {'source': source, 'target': entry, 'registry': REG,
              'original_target': target, 'state': 'parked-pending-verification'}
    (dest / 'promotion.json').write_text(json.dumps(record, indent=2) + '\n')
    os.chmod(dest / 'promotion.json', 0o600)
    d['sessions'] = [entry if e.get('name') == name else e for e in d['sessions']]
    save(d)
    if next(e for e in load()['sessions'] if e.get('name') == name) != entry:
        raise ValueError('promotion readback mismatch; preserve manifest for recovery')
    return entry


def rollback_promotion(manifest):
    """Restore only the source row; caller must park and close the target first."""
    record = json.loads(pathlib.Path(manifest).read_text())
    if os.path.realpath(record['registry']) != os.path.realpath(REG):
        raise ValueError('rollback registry mismatch')
    d = load()
    name = record['source']['name']
    current = next((e for e in d['sessions'] if e.get('name') == name), None)
    if not current or current.get('uuid') != record['target']['uuid']:
        raise ValueError('rollback target identity changed')
    if current.get('desired') != 'parked' or _probe_live(name):
        raise ValueError('park and close target before rollback')
    d['sessions'] = [record['source'] if e.get('name') == name else e for e in d['sessions']]
    save(d)
    return record['source']


def slug(cwd):
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.expanduser(cwd))

def transcript_path(e):
    if _harness.resolve(e).get("provider") == "codex":
        import fleet_transcript
        return fleet_transcript.resolve_codex(e)[0]
    cfg = os.path.expanduser(e.get("config_dir") or CFG.get(e.get("profile", "work")))
    base = pathlib.Path(cfg) / "projects" / slug(e["cwd"])
    if e.get("uuid"):
        p = base / f"{e['uuid']}.jsonl"
        return str(p) if p.is_file() else None
    cand = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return os.fspath(cand[0]) if cand else None

def _recover_uuid(e):
    """Recover from a live process binding or one unambiguous project transcript."""
    if _harness.resolve(e).get("provider") == "codex":
        import fleet_transcript
        path, _ = fleet_transcript.resolve_codex(e)
        meta = fleet_transcript.codex_metadata(path) if path else None
        return (meta or {}).get("session_id")

    import importlib
    fleet_transcript = importlib.import_module("fleet_transcript")
    path, source = fleet_transcript.resolve(e, tmux=e.get("name"), use_cache=False)
    if path and source in {"uuid", "process"}:
        return pathlib.Path(path).stem

    cfg = os.path.expanduser(e.get("config_dir") or CFG.get(e.get("profile", "work")))
    base = pathlib.Path(cfg) / "projects" / slug(e["cwd"])
    candidates = list(base.glob("*.jsonl"))
    if len(candidates) != 1:
        return None
    import uuid
    try:
        uuid.UUID(candidates[0].stem)
    except ValueError:
        return None
    return candidates[0].stem

def _discover_codex_rollout(root, cwd, since, before=()):
    """Return the sole fresh, UUID-identified Codex thread for cwd."""
    import uuid
    import fleet_transcript as _ft
    before = {pathlib.Path(p).resolve() for p in before}
    matches = []
    for candidate in pathlib.Path(root).glob("**/*.jsonl"):
        try:
            if candidate.resolve() in before or candidate.stat().st_mtime < since - 2:
                continue
            meta = _ft.codex_metadata(str(candidate)) or {}
            sid = meta.get("session_id")
            if sid and os.path.realpath(meta.get("cwd", "")) == os.path.realpath(cwd):
                try:
                    uuid.UUID(str(sid))
                except (ValueError, TypeError, AttributeError):
                    continue
                matches.append(sid)
        except OSError:
            continue
    return matches[0] if len(matches) == 1 else None

STALE_HOURS = float(os.environ.get("FLEET_STALE_HOURS", "24"))


def _quiet_for(path):
    """Seconds since the transcript was last written, or None if unreadable."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except Exception:
        return None


def _fmt_quiet(secs):
    if secs is None:
        return "?"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _probe_live(name):
    """A managed session is ALIVE iff its tmux session exists (tmux IS the pane; when the
    claude proc dies the window closes and the session dies)."""
    try:
        return subprocess.run(["tmux", "has-session", "-t", "=" + name + ":"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _rc_dir(cwd):
    """RC socket dir for a cwd: /tmp/claude-<uid>/<encoded-cwd>. None if cwd unset/empty."""
    cwd = os.path.expanduser(cwd or "")
    if not cwd:
        return None
    enc = "-" + cwd.lstrip("/").replace("/", "-")
    return pathlib.Path(f"/tmp/claude-{os.getuid()}/{enc}")


def _main_procs(e):
    """Lines of claude MAIN procs for e: processes whose command token is `claude` and whose
    argv has `--resume <uuid>`. Excludes the tmux wrapper (command == tmux)."""
    uuid = e.get("uuid")
    if not uuid:
        return []
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    res = []
    for ln in out.splitlines()[1:]:
        toks = ln.split()
        if len(toks) < 2:
            continue
        cmd = toks[1]
        if cmd.endswith("/claude") or cmd == "claude":
            if f"--resume {uuid}" in ln:
                res.append(ln)
    return res


def hygiene(clean=False):
    """Detect (and optionally clean) the RC-hygiene failure class: duplicate main claude
    procs per session + stale RC sockets. Root-causes 'RC not working' drift where a
    session accumulates >1 --resume claude process / orphan sockets so the remote control
    surface is ambiguous or dead."""
    reg = load()["sessions"]
    alluuids = {e.get("uuid") or "" for e in reg}
    changed = False
    for e in reg:
        name, uuid = e["name"], e.get("uuid")
        procs = _main_procs(e)
        excess = len(procs) - 1
        st = "OK  " if (excess <= 0) else f"DUP={excess}"
        print(f"{st} {name:26} claude_main={len(procs)}")
        if clean and excess > 0:
            # kill all but the last (newest) main proc; tmux pane is unchanged
            for ln in procs[:-1]:
                pid = ln.split()[0]
                subprocess.run(["kill", "-TERM", pid])
                print(f"    killed duplicate main proc {pid}")
                changed = True
        # stale RC sockets: any entry (socket file OR per-uuid subdir) in the session's rc
        # dir whose uuid is NOT a currently-registered session = orphan from a past run.
        # NEVER touch a live session's entry (uuid in alluuids), so this is safe by construction.
        d = _rc_dir(e.get("cwd", ""))
        if d and d.is_dir():
            for sock in sorted(d.iterdir()):
                if sock.name in alluuids:
                    continue
                print(f"    stale RC entry: {sock.name[:12]} ({d.name})")
                if clean:
                    try:
                        if sock.is_dir():
                            import shutil; shutil.rmtree(sock)
                        else:
                            sock.unlink()
                        changed = True
                        print("      removed")
                    except FileNotFoundError:
                        pass
    return changed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    hyg = sub.add_parser("hygiene")
    hyg.add_argument("--clean", action="store_true",
                     help="kill duplicate main claude procs + remove stale RC sockets per session")
    reg = sub.add_parser("register")
    reg.add_argument("name"); reg.add_argument("--short", required=True)
    reg.add_argument("--profile", required=True, choices=list(CFG))
    reg.add_argument("--provider", choices=("claude", "codex"))
    reg.add_argument("--codex-home")
    reg.add_argument("--registry", help="registry path override for isolated registration")
    reg.add_argument("--source-provenance-file", help="pending Codex handoff manifest")
    reg.add_argument("--handoff-resolved", action="store_true",
                     help="confirm an owner-input startup prompt was resolved")
    reg.add_argument("--cwd", required=True); reg.add_argument("--uuid")
    reg.add_argument("--force", action="store_true",
                     help="register even if no session transcript resolves (you'll own the unrecoverable risk)")
    unreg = sub.add_parser("unregister"); unreg.add_argument("name")
    promotion = sub.add_parser("promote", help="publish a stopped provider canary under a parked source name")
    promotion.add_argument("name")
    promotion.add_argument("--candidate-file", required=True)
    promotion.add_argument("--expected-source-uuid", required=True)
    promotion.add_argument("--transaction-dir", required=True)
    rollback = sub.add_parser("rollback-promotion")
    rollback.add_argument("manifest")
    chk = sub.add_parser("check")
    chk.add_argument("--live", action="store_true", help="also probe every session's tmux liveness (ALIVE/DEAD)")
    sp = sub.add_parser("spawn", help="register a NEW session and launch it")
    sp.add_argument("name"); sp.add_argument("--short", required=True)
    sp.add_argument("--profile", required=True, choices=list(CFG))
    sp.add_argument("--provider", choices=("claude", "codex"))
    sp.add_argument("--codex-home")
    sp.add_argument("--cwd", required=True)
    a = ap.parse_args()
    global REG
    if getattr(a, "registry", None):
        REG = os.path.expanduser(a.registry)
    if getattr(a, "codex_home", None) and getattr(a, "provider", None) != "codex":
        ap.error("--codex-home requires --provider codex")
    if getattr(a, "source_provenance_file", None) and getattr(a, "provider", None) != "codex":
        ap.error("--source-provenance-file requires --provider codex")
    if getattr(a, "handoff_resolved", False) and not getattr(a, "source_provenance_file", None):
        ap.error("--handoff-resolved requires --source-provenance-file")
    d = load()
    if a.cmd == "promote":
        target = json.loads(pathlib.Path(a.candidate_file).read_text())
        print(json.dumps(promote(a.name, target, a.expected_source_uuid, a.transaction_dir), indent=2))
    elif a.cmd == "rollback-promotion":
        print(json.dumps(rollback_promotion(a.manifest), indent=2))
    elif a.cmd == "list":
        for e in d["sessions"]:
            live = "ALIVE" if _probe_live(e["name"]) else "DEAD"
            print(f"{e['name']:22} {e['short']:14} {e['profile']:9} {e['cwd']}  [{live}]")
    elif a.cmd == "register":
        entry = {"name": a.name, "short": a.short, "profile": a.profile,
                 "config_dir": CFG[a.profile], "cwd": a.cwd}
        _provider_entry(entry, a.provider, a.codex_home)
        if a.source_provenance_file:
            try:
                payload = json.loads(pathlib.Path(os.path.expanduser(a.source_provenance_file)).read_text())
                if not isinstance(payload, dict):
                    raise ValueError("pending handoff manifest must be a JSON object")
                if payload.get("target_name") and payload["target_name"] != a.name:
                    raise ValueError("pending handoff target name does not match registration")
                path_pairs = (("cwd", entry["cwd"]), ("codex_home", entry["codex_home"]),
                              ("registry", REG))
                for field, expected in path_pairs:
                    recorded = payload.get(field)
                    if recorded and os.path.realpath(os.path.expanduser(recorded)) != os.path.realpath(os.path.expanduser(expected)):
                        raise ValueError(f"pending handoff {field} does not match registration")
                provenance = payload.get("source_provenance")
                if (not isinstance(provenance, dict) or provenance.get("provider") != "claude" or
                        not provenance.get("session_id") or not provenance.get("transcript") or
                        not provenance.get("handoff_context")):
                    raise ValueError("source provenance is incomplete")
                if not pathlib.Path(provenance["transcript"]).is_file():
                    raise ValueError("source transcript is missing")
                if not pathlib.Path(provenance["handoff_context"]).is_file():
                    raise ValueError("handoff context is missing")
                if payload.get("state") in ("NEEDS-INPUT", "UNKNOWN") and not a.handoff_resolved:
                    raise ValueError("owner-input state must be resolved before registration")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                print(f"REFUSED: invalid handoff provenance: {exc}", file=sys.stderr)
                sys.exit(2)
            entry["source_provenance"] = provenance
        entry["desired"] = "up"
        if a.uuid:
            entry["uuid"] = a.uuid
        else:
            # class-fix: recover a captured-uuid-not-never; don't register a phantom.
            found = _recover_uuid(entry)
            if found:
                entry["uuid"] = found
                print(f"[auto-recovered uuid {found} for {a.name}]")
        if (not entry.get("uuid") or not transcript_path(entry)) and not a.force:
            print(f"REFUSED: no unambiguous session UUID and transcript resolve for {a.name} "
                  "(would be unrecoverable at reboot). Pass --uuid or --force to accept that risk.")
            sys.exit(1)
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name] + [entry]
        save(d); print(f"registered {a.name} -> {transcript_path(entry) or a.name+' (NO transcript -- unrecoverable)'}")
    elif a.cmd == "unregister":
        n0 = len(d["sessions"])
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name]
        save(d); print(f"unregistered {a.name}" if len(d["sessions"]) < n0 else f"not found: {a.name}")
    elif a.cmd == "hygiene":
        sys.exit(0 if not hygiene(clean=a.clean) else 0)
    elif a.cmd == "spawn":
        entry = {"name": a.name, "short": a.short, "profile": a.profile,
                 "config_dir": CFG[a.profile], "cwd": a.cwd, "desired": "up"}
        _provider_entry(entry, a.provider, a.codex_home)
        cfg = os.path.expanduser(entry["config_dir"])
        cwd = os.path.expanduser(a.cwd) or cfg
        launch = _harness.shell_line(entry)
        t0 = time.time()
        resolved = _harness.resolve(entry)
        if resolved.get("provider") == "codex":
            entry["codex_home"] = resolved.get("env", {}).get("CODEX_HOME") or cfg
        codex_root = pathlib.Path(resolved.get("env", {}).get("CODEX_HOME") or
                                  entry.get("codex_home") or cfg).expanduser() / "sessions"
        before_codex = {p.resolve() for p in codex_root.glob("**/*.jsonl")} if codex_root.exists() else set()
        subprocess.run(["tmux", "new-session", "-d", "-s", a.name, "-c", cwd, launch],
                       check=False)
        time.sleep(5)
        # Trust prompt: the default is the option that ENDS the session, so move up first.
        pane = subprocess.run(["tmux", "capture-pane", "-t", "=" + a.name + ":", "-p"],
                              capture_output=True, text=True).stdout or ""
        is_codex = _harness.resolve(entry).get("provider") == "codex"
        if not is_codex and ("trust this folder" in pane.lower() or "No, exit" in pane):
            subprocess.run(["tmux", "send-keys", "-t", "=" + a.name + ":", "Down"],
                           capture_output=True)
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", "=" + a.name + ":", "Enter"],
                           capture_output=True)
            time.sleep(3)
        blocker = None
        if is_codex:
            import fleet_state as _fleet_state
            blocker = _fleet_state.codex_startup_blocker(pane)
            newuuid = _discover_codex_rollout(codex_root, cwd, t0, before_codex)
        else:
            import glob as _glob
            slug = cwd.replace("/", "-").strip("-")
            cand = sorted(_glob.glob(f"{cfg}/projects/-{slug}/*.jsonl"),
                          key=os.path.getmtime, reverse=True)
            newuuid = next((os.path.basename(p)[:-6] for p in cand
                            if os.path.getmtime(p) >= t0 - 2), None)
        if is_codex and not newuuid:
            status = "NEEDS-INPUT" if blocker else "UNKNOWN"
            reason = blocker or "no unique native rollout found; startup state needs owner inspection"
            print(f"{status}: {reason}; left tmux session {a.name} running and registry unchanged")
            sys.exit(2)
        entry["uuid"] = newuuid
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name] + [entry]
        save(d)
        print(f"spawned {a.name} -> uuid {newuuid or '(unresolved)'}")
        if blocker:
            print(f"  NEEDS-INPUT: {blocker}")
        print(f"  cwd={cwd}  profile={a.profile}  transcript="
              f"{transcript_path(entry) or '(none yet)'}")
    elif a.cmd == "check":
        miss = nlimited = nstale = 0
        ust = {}
        try:
            ust = json.load(open(os.path.expanduser(
                "~/.hermes/scripts/cc-watch/fleet_watch_state.json"))) or {}
        except Exception:
            ust = {}
        ust = ust.get("_usage_limit", {})
        for e in d["sessions"]:
            p = transcript_path(e)
            lv = "ALIVE" if _probe_live(e["name"]) else "DEAD"
            u = ust.get(e["name"], {})
            lim = "LIMIT" if u.get("limited") else "OK  "
            if u.get("limited"): nlimited += 1
            tail = f"reset {u['reset']}" if (u.get("limited") and u.get("reset")) else ""
            if p:
                q = _quiet_for(p)
                stale = lv == "ALIVE" and q is not None and q > STALE_HOURS * 3600
                if stale:
                    nstale += 1
                mark = " STALE" if stale else ""
                print(f"OK   {e['short']:12} {lv:5} {lim:5} {p} {tail}"
                      f"  [quiet {_fmt_quiet(q)}{mark}]")
            else:
                miss += 1; print(f"MISS {e['short']:12} {lv:5} {lim:5} no transcript ({e['name']})")
        ndead = sum(1 for e in d["sessions"] if not _probe_live(e["name"]))
        print(f"{len(d['sessions'])} registered, {miss} missing transcript, {ndead} not-alive, "
              f"{nlimited} usage-limited, {nstale} alive-but-quiet over {STALE_HOURS:g}h")
        sys.exit(1 if (miss or ndead) else 0)

if __name__ == "__main__":
    main()
