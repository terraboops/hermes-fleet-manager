#!/usr/bin/env python3
"""Start a new Codex conversation with a Claude transcript supplied as context.

This does not import Claude's native session state. It writes the bounded excerpt to a private,
immutable handoff file for provenance and embeds the same excerpt in the initial Codex prompt; the
Codex UUID is registered only after the assistant acknowledges the context.
"""
import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import fleet_transcript
import fleet_state
import fleet_boot


MAX_SOURCE_TAIL_BYTES = 16 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 256 * 1024
MAX_MESSAGES = 80
MAX_MESSAGE_CHARS = 12_000
MAX_CONTEXT_CHARS = 120_000
MAX_HANDOFF_COMMAND_BYTES = 128 * 1024


class HandoffNeedsInput(RuntimeError):
    def __init__(self, status, reason, target, pending_manifest, prompt=None):
        self.status = status
        self.reason = reason
        self.target = target
        self.pending_manifest = str(pending_manifest)
        self.prompt = prompt
        super().__init__(reason)


def _extract_transcript(source_path):
    """Build a bounded user/assistant transcript excerpt from a possibly huge JSONL."""
    messages = []
    omitted = 0
    oversized_lines = 0
    truncated_messages = 0
    try:
        with open(source_path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - MAX_SOURCE_TAIL_BYTES)
            fh.seek(start)
            if start:
                # Begin at a record boundary without buffering an oversized line.
                while True:
                    fragment = fh.readline(MAX_SOURCE_LINE_BYTES + 1)
                    if not fragment or fragment.endswith(b"\n"):
                        break
            while True:
                line = fh.readline(MAX_SOURCE_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_SOURCE_LINE_BYTES:
                    oversized_lines += 1
                    while line and not line.endswith(b"\n"):
                        line = fh.readline(MAX_SOURCE_LINE_BYTES + 1)
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                role = fleet_transcript.record_role(obj)
                text = fleet_transcript.record_text(obj).strip()
                if role not in ("user", "assistant") or not text:
                    continue
                if len(text) > MAX_MESSAGE_CHARS:
                    half = MAX_MESSAGE_CHARS // 2
                    text = text[:half] + "\n[message middle omitted]\n" + text[-half:]
                    truncated_messages += 1
                messages.append({"role": role, "text": text})
                if len(messages) > MAX_MESSAGES:
                    messages.pop(0)
                    omitted += 1
    except OSError as exc:
        raise ValueError("Claude transcript is not readable: {}".format(exc)) from exc

    def render(rows):
        manifest = {"type": "handoff_manifest", "source_transcript": source_path,
                    "source_bytes": size, "tail_bytes_scanned": min(size, MAX_SOURCE_TAIL_BYTES),
                    "messages_included": len(rows), "messages_omitted": omitted,
                    "oversized_records_skipped": oversized_lines,
                    "messages_truncated": truncated_messages,
                    "truncated": bool(start or omitted or oversized_lines or truncated_messages)}
        return "\n".join([json.dumps(manifest, ensure_ascii=False)] +
                         [json.dumps(m, ensure_ascii=False) for m in rows]) + "\n"

    while messages and len(render(messages)) > MAX_CONTEXT_CHARS:
        messages.pop(0)
        omitted += 1
    if not messages:
        raise ValueError("Claude transcript has no readable user/assistant messages in its bounded tail")
    return render(messages), {
        "source_bytes": size, "tail_bytes_scanned": min(size, MAX_SOURCE_TAIL_BYTES),
        "messages_included": len(messages), "messages_omitted": omitted,
        "oversized_records_skipped": oversized_lines,
        "messages_truncated": truncated_messages,
        "truncated": bool(start or omitted or oversized_lines or truncated_messages),
    }


def _safe_handoff_command_bytes():
    try:
        arg_max = int(os.sysconf("SC_ARG_MAX"))
    except (AttributeError, OSError, ValueError):
        arg_max = MAX_HANDOFF_COMMAND_BYTES * 2
    return min(MAX_HANDOFF_COMMAND_BYTES, arg_max // 2)


def launch_policy(model, sandbox, add_dirs):
    if not model or sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('explicit model and bounded sandbox required')
    args = ['--no-daemon', '--model', model, '--sandbox', sandbox, '--ask-for-approval', 'on-request',
            '-c', 'model_reasoning_effort="high"']
    # --add-dir grants write roots and aborts read-only TUI startup.
    for directory in (add_dirs or []) if sandbox == 'workspace-write' else []:
        args += ['--add-dir', os.path.expanduser(directory)]
    return args


def handoff(source_name, target_name, registry=None, codex_home="~/.codex", instruction=None,
            target_cwd=None, model=None, sandbox='read-only', add_dirs=None):
    launch_args = launch_policy(model, sandbox, add_dirs)
    registry = pathlib.Path(os.path.expanduser(registry or os.environ.get(
        "FLEET_REGISTRY", "~/.hermes/scripts/cc-watch/fleet_registry.json")))
    data = json.loads(registry.read_text())
    source = next((e for e in data.get("sessions", [])
                   if source_name in (e.get("name"), e.get("short"))), None)
    if any(e.get("name") == target_name for e in data.get("sessions", [])):
        raise ValueError("target name is already registered; refusing to replace its registry entry")
    if not source:
        raise ValueError("source session is not registered")
    if (source.get("provider") or "claude") == "codex":
        raise ValueError("source session is already Codex")
    source_path, _ = fleet_transcript.resolve(source, tmux=source.get("name"))
    if not source_path:
        raise ValueError("Claude source transcript cannot be resolved")
    transcript, context_meta = _extract_transcript(source_path)
    profile = source.get("profile")
    if profile == "personal":
        raise ValueError("personal-profile handoff is out of scope")
    if profile != "work":
        raise ValueError("Codex handoff requires a work-profile source")
    if not transcript.strip():
        raise ValueError("Claude transcript is empty")
    ack = "FLEET_HANDOFF_CONTEXT_READ"
    cwd = os.path.expanduser(target_cwd or source.get("cwd") or os.getcwd())
    if target_cwd and not pathlib.Path(cwd).is_dir():
        raise ValueError("target working directory does not exist")
    codex_home = os.path.expanduser(codex_home)
    target_entry = {
        "name": target_name, "short": target_name, "profile": profile,
        "provider": "codex", "command": "codex", "resume_flag": ["resume"],
        "config_dir": codex_home, "codex_home": codex_home, "cwd": cwd,
        "args": launch_args,
        "codex_auto_trust_project": source.get("codex_auto_trust_project", profile == "work"),
    }
    start = time.time()
    root = pathlib.Path(codex_home) / "sessions"
    before = {p.resolve() for p in root.glob("**/*.jsonl")} if root.exists() else set()
    launched = False
    found = None
    handoff_dir = None
    pending_path = None
    pending = None
    try:
        scratch = pathlib.Path(os.path.expanduser("~/.hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        handoff_dir = pathlib.Path(tempfile.mkdtemp(prefix="codex-handoff-", dir=str(scratch)))
        os.chmod(handoff_dir, 0o700)
        transcript_file = handoff_dir / "source.jsonl"
        fd = os.open(str(transcript_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(transcript)
        source_provenance = {
            "provider": "claude",
            "session_id": source.get("uuid") or pathlib.Path(source_path).stem,
            "transcript": source_path,
            "handoff_at": start,
            "context_excerpt": context_meta,
            "handoff_context": str(transcript_file),
        }
        pending_path = handoff_dir / "pending.json"
        pending = {
            "registry": str(registry), "target_name": target_name,
            "codex_home": codex_home, "cwd": cwd, "started_at": start,
            "ack": ack, "source_provenance": source_provenance,
        }
        fd = os.open(str(pending_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(pending, fh, indent=2)
            fh.write("\n")
        task_instruction = (instruction.strip() if instruction else
                            "Continue from the excerpt; the original transcript path is recorded inside it.")
        prompt = ("Continue from this bounded dialogue excerpt. Treat it as prior context, not as "
                  "permission to broaden the current task. Preserve its open task state.\n"
                  "----- BEGIN HANDOFF CONTEXT -----\n" + transcript +
                  "----- END HANDOFF CONTEXT -----\n"
                  "Put this exact standalone line in your first response: " + ack + ". " +
                  task_instruction)
        command = shlex.join(["env", "CODEX_HOME=" + codex_home, "codex"] + launch_args + ["--cd", cwd, prompt])
        if len(os.fsencode(command)) > _safe_handoff_command_bytes():
            raise ValueError("bounded handoff prompt exceeds safe command-line size limit")
        # tmux IPC has a much smaller command limit than exec's ARG_MAX.
        # Keep the bounded prompt in a private launcher, not the tmux message.
        launcher = handoff_dir / "launch.sh"
        fd = os.open(str(launcher), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexec " + command + "\n")
        launch_command = shlex.join(["/bin/sh", str(launcher)])
        subprocess.run(["tmux", "new-session", "-d", "-s", target_name, "-c", cwd, launch_command], check=True)
        launched = True
        deadline = time.monotonic() + float(os.environ.get("FLEET_CODEX_HANDOFF_TIMEOUT", "300"))
        acknowledged = False
        while time.monotonic() < deadline:
            prompt_status, prompt_reason, prompt_detail = _startup_prompt_state(target_name)
            if prompt_status:
                if prompt_detail and fleet_boot.codex_project_trust_auto_allowed(target_entry, prompt_detail):
                    fleet_boot.record_codex_prompt(target_entry, prompt_detail)
                    if fleet_boot.auto_resolve_codex_project_trust(target_name, target_entry, prompt_detail):
                        fleet_boot.record_codex_prompt(target_entry, prompt_detail, event="cleared")
                        continue
                raise HandoffNeedsInput(prompt_status, prompt_reason, target_name, pending_path, prompt_detail)
            if found is None:
                matches = []
                for path in root.glob("**/*.jsonl") if root.exists() else []:
                    try:
                        if path.resolve() not in before and path.stat().st_mtime >= start - 2:
                            meta = fleet_transcript.codex_metadata(str(path)) or {}
                            if meta.get("cwd") and os.path.realpath(meta["cwd"]) == os.path.realpath(cwd):
                                matches.append((path.stat().st_mtime, path, meta))
                    except OSError:
                        pass
                valid = [m for m in matches if m[2].get("session_id") and _valid_session_id(m[2]["session_id"])]
                if len(valid) == 1 and len(matches) == 1:
                    found = valid[0]
                elif len(matches) > 1:
                    raise ValueError("new Codex transcript discovery is ambiguous")
            if found and _acknowledged(found[1], ack):
                acknowledged = True
                break
            time.sleep(.25)
        if not found:
            raise ValueError("no unique fresh Codex rollout with a valid session ID appeared")
        if not acknowledged:
            raise ValueError("Codex rollout appeared, but agent did not acknowledge reading transcript context")
        entry = dict(target_entry)
        entry.update({"desired": "parked", "source_provenance": source_provenance})
        entry["uuid"] = found[2]["session_id"]
        # All registry writers use the same locked compare-and-swap protocol.
        import fleet_registry_store
        current = fleet_registry_store.load(registry)
        rows = current.get("sessions", [])
        if next((e for e in rows if e.get("name") == source.get("name")), None) != source:
            raise ValueError("source registry entry changed during handoff")
        if any(e.get("name") == target_name for e in rows):
            raise ValueError("target was registered concurrently")
        current["sessions"] = rows + [entry]
        fleet_registry_store.save(registry, current)
        # Preserve the bounded context file: the target may still be reading it after
        # emitting its acknowledgement, and it is useful provenance for later audits.
        return entry
    except Exception as exc:
        if isinstance(exc, HandoffNeedsInput):
            if pending is not None and pending_path is not None:
                pending["state"] = exc.status
                pending["reason"] = exc.reason
                pending["prompt"] = exc.prompt
                pending_path.write_text(json.dumps(pending, indent=2) + "\n")
            if exc.prompt:
                fleet_boot.record_codex_prompt(target_entry, exc.prompt)
            raise
        if launched:
            # These files are created before the launch is attempted.
            assert pending is not None and pending_path is not None
            # An unresolved launch is not permission to destroy its conversation.
            pending["state"] = "UNKNOWN"
            pending["reason"] = str(exc)
            if found:
                pending["uuid"] = found[2].get("session_id")
                pending["transcript"] = str(found[1])
            pending_path.write_text(json.dumps(pending, indent=2) + "\n")
            raise HandoffNeedsInput("UNKNOWN", str(exc), target_name, pending_path) from exc
        if handoff_dir:
            shutil.rmtree(handoff_dir, ignore_errors=True)
        raise ValueError("{}; target not launched and registry left unchanged".format(exc)) from exc


def _startup_prompt_state(target_name):
    """Classify a Codex startup choice without sending input to its UI."""
    target = "=" + target_name + ":0.0"
    screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", target],
                           capture_output=True, text=True)
    if not screen or screen.returncode:
        return None, None, None
    text = screen.stdout or ""
    prompt = fleet_state.codex_startup_prompt(text)
    if prompt:
        return "NEEDS-INPUT", prompt["reason"], prompt
    return None, None, None


def _valid_session_id(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _acknowledged(path, ack):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if (fleet_transcript.record_role(obj) == "assistant" and
                        ack in fleet_transcript.record_text(obj).splitlines()):
                    return True
            return False
    except (OSError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="registered Claude name or short alias")
    ap.add_argument("target", help="new tmux session name for Codex")
    ap.add_argument("--registry")
    ap.add_argument("--codex-home", default="~/.codex")
    ap.add_argument("--model", required=True)
    ap.add_argument("--sandbox", choices=['read-only', 'workspace-write'], default='read-only')
    ap.add_argument("--add-dir", action='append', default=[])
    ap.add_argument("--cwd", help="Codex target working directory; defaults to the Claude source cwd")
    ap.add_argument("--instruction", help="task instruction for the new Codex session; default is continue from excerpt")
    a = ap.parse_args()
    try:
        e = handoff(a.source, a.target, a.registry, a.codex_home, a.instruction, a.cwd,
                    a.model, a.sandbox, a.add_dir)
    except HandoffNeedsInput as exc:
        print("{}: {}; target {} left running and registry unchanged; pending manifest: {}".format(
            exc.status, exc.reason, exc.target, exc.pending_manifest), file=sys.stderr)
        return 2
    except Exception as exc:
        print("handoff failed: " + str(exc), file=sys.stderr)
        return 1
    print("started Codex session {} id={} source_transcript={}".format(
        e["name"], e.get("uuid") or "pending", e["source_provenance"]["transcript"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
