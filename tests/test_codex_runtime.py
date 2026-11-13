import contextlib
import importlib.util
import json
import os
import pathlib
import shutil
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import fleet_harness
import fleet_transcript
import codex_handoff
import fleet_boot
import fleet_reg
import fleet_state


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "sessions" / "2026" / "10" / "26"
        self.root.mkdir(parents=True)
        shutil.copy(ROOT / "tests/fixtures/codex_rollout.jsonl", self.root / "rollout.jsonl")

    def test_explicit_codex_provider_overrides_stale_global_config(self):
        path = str(self.root / "rollout.jsonl")
        entry = {"name": "codex-example", "profile": "codex", "provider": "codex",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83",
                 "codex_home": self.tmp, "cwd": "/tmp/project"}
        with mock.patch.object(fleet_transcript, "_provider", return_value="claude"):
            with mock.patch.object(fleet_transcript, "resolve_codex", return_value=(path, "session_id")):
                resolved, source = fleet_transcript.resolve(entry)
        self.assertEqual((resolved, source), (path, "session_id"))

    def test_codex_state_uses_native_identity_and_codex_prompt_for_idle(self):
        entry = {"name": "codex-one", "short": "one", "provider": "codex", "profile": "codex",
                 "codex_home": self.tmp, "cwd": "/tmp/project",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83"}
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [entry]}))
        with mock.patch.object(fleet_state, "REGISTRY", str(registry)):
            idle = fleet_state.codex_state("codex-one", entry, "• response complete\n› Ask Codex to do anything")
            working = fleet_state.codex_state("codex-one", entry, "• Thinking...\nesc to interrupt")
            unknown = fleet_state.codex_state("codex-one", entry, "Codex status unavailable")
        self.assertTrue(idle.startswith("IDLE|01a17998-dd2|assistant"))
        self.assertTrue(working.startswith("WORKING|01a17998-dd2|assistant"))
        self.assertTrue(unknown.startswith("UNKNOWN|01a17998-dd2|assistant"))

    def test_codex_monitor_fingerprint_omits_volatile_inbound_age(self):
        entry = {"name": "codex-one", "provider": "codex", "profile": "codex",
                 "codex_home": self.tmp, "cwd": "/tmp/project",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83"}
        transcript = self.root / "rollout.jsonl"
        pane = "• response complete\n› Ask Codex to do anything"
        with mock.patch.object(fleet_state, "transcript_of", return_value=str(transcript)), \
             mock.patch.object(fleet_transcript, "newest_inbound", return_value=(100, "same request")), \
             mock.patch.object(fleet_state.time, "time", side_effect=[1000, 1001]):
            first = fleet_state.codex_state("codex-one", entry, pane)
            second = fleet_state.codex_state("codex-one", entry, pane)
        self.assertEqual(first, second)
        self.assertIn("|u=", first)
        self.assertNotIn("|in=", first)

    def test_codex_startup_prompts_are_needs_input(self):
        entry = {"name": "codex-one", "short": "one", "provider": "codex", "profile": "codex",
                 "codex_home": self.tmp, "cwd": "/tmp/project",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83"}
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [entry]}))
        screens = [
            ("Update available · 0.162.1 → 0.164.0\\n› 1. Update now\\n2. Skip\\nenter continue · esc skip",
             "Codex update prompt requires an owner decision (0.162.1 → 0.164.0)"),
            ("Hooks need review\\n2 hooks are new or changed\\n› 1. Review hooks\\n2. Trust all and continue\\n3. Continue without trusting (hooks won't run)\\nenter confirm · esc skip",
             "Codex hook review requires an owner decision (2 changed hooks)"),
            ("Sign in with ChatGPT to use Codex\\n1. Sign in with ChatGPT",
             "Codex sign-in prompt requires an owner"),
        ]
        with mock.patch.object(fleet_state, "REGISTRY", str(registry)):
            for pane, reason in screens:
                with self.subTest(reason=reason):
                    self.assertEqual(fleet_state.codex_startup_blocker(pane), reason)
                    self.assertTrue(fleet_state.codex_state("codex-one", entry, pane).startswith(
                        "NEEDS-INPUT|01a17998-dd2|assistant"))

    def test_codex_startup_prompt_exposes_update_hook_and_trust_choices(self):
        screens = [
            ("Update available · 0.162.1 → 0.164.0\\n"
             "› 1. Update now (runs `npm install -g @openai/codex`)\\n"
             "  2. Skip\\n  3. Skip until next version\\nenter continue · esc skip",
             "update", ["Update now", "Skip", "Skip until next version"],
             {"from_version": "0.162.1", "to_version": "0.164.0"}),
            ("Hooks need review\\n2 hooks are new or changed\\n"
             "› 1. Review hooks\\n  2. Trust all and continue\\n"
             "  3. Continue without trusting (hooks won't run)\\nenter confirm · esc skip",
             "hook-review", ["Review hooks", "Trust all and continue", "Continue without trusting"],
             {"changed_hooks": 2}),
            ("Folder access\\nTrust this folder? Codex can read, edit, and run files here.\\n"
             "› 1. Trust and continue\\n  2. Back to Agent Command Center\\nenter continue · esc back",
             "project-trust", ["Trust and continue", "Back to Agent Command Center"], {}),
        ]
        for screen, category, choices, details in screens:
            with self.subTest(category=category):
                prompt = fleet_state.codex_startup_prompt(screen)
                self.assertEqual(prompt["category"], category)
                self.assertEqual(prompt["choices"], choices)
                self.assertEqual(prompt["details"], details)
                self.assertEqual(prompt["selected_choice"], choices[0])
                self.assertTrue(prompt["fingerprint"])
                self.assertEqual(fleet_state.codex_startup_blocker(screen), prompt["reason"])

    def test_codex_prompt_event_log_records_category_without_raw_screen_or_identity(self):
        screen = ("Folder access\\nTrust this folder? Codex can read, edit, and run files here.\\n"
                  "› 1. Trust and continue\\n  2. Back to Agent Command Center")
        prompt = fleet_state.codex_startup_prompt(screen)
        entry = {"name": "private-session-name", "uuid": "private-session-uuid"}
        log_path = pathlib.Path(self.tmp) / "codex-prompt-events.jsonl"
        event = fleet_state.record_codex_prompt_event(entry, prompt, path=str(log_path))
        self.assertTrue(event)
        record = json.loads(log_path.read_text())
        self.assertEqual(record["category"], "project-trust")
        self.assertEqual(record["choices"], ["Trust and continue", "Back to Agent Command Center"])
        self.assertNotIn("private-session-name", log_path.read_text())
        self.assertNotIn("private-session-uuid", log_path.read_text())
        self.assertNotIn("Trust this folder?", log_path.read_text())

    def test_codex_prompt_event_log_records_automatic_resolution(self):
        screen = ("Folder access\\nTrust this folder?\\n"
                  "› 1. Trust and continue\\n  2. Back to Agent Command Center")
        prompt = fleet_state.codex_startup_prompt(screen)
        log_path = pathlib.Path(self.tmp) / "codex-auto-prompt-events.jsonl"
        self.assertTrue(fleet_state.record_codex_prompt_event(
            {"name": "codex-one"}, prompt, event="auto-resolved", path=str(log_path)))
        record = json.loads(log_path.read_text())
        self.assertEqual(record["event"], "auto-resolved")
        self.assertEqual(record["selected_choice"], "Trust and continue")
        self.assertNotIn("Trust this folder?", log_path.read_text())

    def test_codex_state_marks_unanswered_turn_unknown_without_activity_cue(self):
        entry = {"name": "codex-one", "provider": "codex", "profile": "codex",
                 "codex_home": self.tmp, "cwd": "/tmp/project",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83"}
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [entry]}))
        path = self.root / "rollout.jsonl"
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [{"type": "input_text", "text": "new request"}]}}) + "\n")
        with mock.patch.object(fleet_state, "REGISTRY", str(registry)):
            result = fleet_state.codex_state("codex-one", entry, "› Ask Codex to do anything")
        self.assertTrue(result.startswith("UNKNOWN|01a17998-dd2|user"))

    def test_fleet_last_resolves_explicit_codex_provider(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fleet_last_test", ROOT / "scripts/fleet_last.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = {"provider": "codex", "profile": "codex", "command": "codex",
                 "uuid": "01a17998-dd24-7222-9682-dc353dfbae83", "codex_home": self.tmp,
                 "cwd": "/tmp/project"}
        with mock.patch.object(module, "_entry", return_value=entry):
            self.assertTrue(module._transcript(entry).endswith("rollout.jsonl"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_hook_prompt_is_detected_without_answering(self):
        screen = mock.Mock(returncode=0, stdout="Hooks need review\\nContinue without trusting")
        with mock.patch.object(codex_handoff.subprocess, "run", return_value=screen) as run:
            status, reason, prompt = codex_handoff._startup_prompt_state("codex-one")
        self.assertEqual(status, "NEEDS-INPUT")
        self.assertIn("hook review", reason.lower())
        self.assertEqual(prompt["category"], "hook-review")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["tmux", "capture-pane"])

    def test_unrecognized_choice_is_owner_input_without_answering(self):
        screen = mock.Mock(returncode=0, stdout="Choose one: continue or exit")
        with mock.patch.object(codex_handoff.subprocess, "run", return_value=screen) as run:
            status, reason, prompt = codex_handoff._startup_prompt_state("codex-one")
        self.assertEqual(status, "NEEDS-INPUT")
        self.assertIn("unrecognized", reason.lower())
        self.assertEqual(prompt["category"], "unknown-choice")
        self.assertEqual(run.call_count, 1)

    def test_provider_launch_and_resume_argv(self):
        cfg = {"profiles": {"codex": {"provider": "codex", "command": "codex",
                                      "resume_flag": ["resume"], "codex_home": self.tmp}}}
        entry = {"profile": "codex", "cwd": "/tmp/project", "uuid": "thread"}
        resolved = fleet_harness.resolve(entry, cfg)
        self.assertEqual(resolved["provider"], "codex")
        self.assertEqual(fleet_harness.shell_line(entry, "abc", cfg=cfg),
                         "CODEX_HOME='{}' codex 'resume' 'abc'".format(self.tmp))
        custom = fleet_harness.resolve({"command": "/opt/tools/codex-wrapper"})
        self.assertEqual(custom["provider"], "claude")
        self.assertEqual(fleet_harness.resolve({"command": "/opt/tools/codex-wrapper",
                                                "provider": "codex"})["provider"], "codex")

    def test_codex_resolution_needs_unique_session_match(self):
        e = {"provider": "codex", "codex_home": self.tmp,
             "uuid": "01a17998-dd24-7222-9682-dc353dfbae83", "cwd": "/tmp/project"}
        path, source = fleet_transcript.resolve_codex(e)
        self.assertTrue(path.endswith("rollout.jsonl"))
        self.assertEqual(source, "session_id")
        duplicate = self.root / "second.jsonl"
        shutil.copy(path, duplicate)
        self.assertEqual(fleet_transcript.resolve_codex(e), (None, "ambiguous"))

    def test_transcript_discovery_uses_launch_resolved_codex_home(self):
        e = {"provider": "codex", "command": "codex", "env": {"CODEX_HOME": self.tmp},
             "config_dir": str(pathlib.Path(self.tmp) / "wrong-home"),
             "uuid": "01a17998-dd24-7222-9682-dc353dfbae83", "cwd": "/tmp/project"}
        path, source = fleet_transcript.resolve_codex(e)
        self.assertTrue(path.endswith("rollout.jsonl"))
        self.assertEqual(source, "session_id")

    def test_spawn_discovery_requires_unique_fresh_valid_id_in_resolved_home(self):
        rollout_root = pathlib.Path(self.tmp) / "custom-home" / "sessions"
        rollout_root.mkdir(parents=True)
        good = rollout_root / "new.jsonl"
        shutil.copy(ROOT / "tests/fixtures/codex_rollout.jsonl", good)
        os.utime(good, None)
        self.assertEqual(fleet_reg._discover_codex_rollout(rollout_root, "/tmp/project", 0),
                         "01a17998-dd24-7222-9682-dc353dfbae83")
        self.assertIsNone(fleet_reg._discover_codex_rollout(
            rollout_root, "/tmp/project", 0, before={good}))
        self.assertIsNone(fleet_reg._discover_codex_rollout(rollout_root / "empty", "/tmp/project", 0))
        duplicate = rollout_root / "also-new.jsonl"
        shutil.copy(good, duplicate)
        os.utime(duplicate, None)
        self.assertIsNone(fleet_reg._discover_codex_rollout(rollout_root, "/tmp/project", 0))
        duplicate.write_text('{"type":"session_meta","payload":{"session_id":"bad","cwd":"/tmp/project"}}\n')
        os.utime(duplicate, None)
        self.assertEqual(fleet_reg._discover_codex_rollout(rollout_root, "/tmp/project", 0),
                         "01a17998-dd24-7222-9682-dc353dfbae83")

    def test_codex_record_parser_and_partial_tail(self):
        path = self.root / "rollout.jsonl"
        rows = path.read_text().splitlines()
        user, assistant = map(json.loads, rows[1:3])
        self.assertEqual(fleet_transcript.record_role(user), "user")
        self.assertIn("DISPATCH-demo", fleet_transcript.record_text(user))
        self.assertEqual(fleet_transcript.record_role(assistant), "assistant")
        self.assertEqual(fleet_transcript.record_text(assistant), "DONE-demo-task")

    def test_personal_profile_handoff_is_rejected_before_launch(self):
        claude_dir = pathlib.Path(self.tmp) / "personal" / "projects" / "project"
        claude_dir.mkdir(parents=True)
        source_path = claude_dir / "source-id.jsonl"
        source_path.write_text('{"type":"user","message":{"role":"user","content":"continue"}}\n')
        registry = pathlib.Path(self.tmp) / "personal-registry.json"
        registry.write_text(json.dumps({"sessions": [{"name": "personal-one", "short": "one",
            "profile": "personal", "config_dir": str(pathlib.Path(self.tmp) / "personal"),
            "cwd": "/tmp/project", "uuid": "source-id"}]}))
        with self.assertRaisesRegex(ValueError, "personal-profile handoff is out of scope"):
            codex_handoff.handoff("one", "codex-one", str(registry), str(pathlib.Path(self.tmp) / "codex"), model="gpt-6.1-sol")

    def test_handoff_starts_new_session_and_persists_source_provenance(self):
        claude_dir = pathlib.Path(self.tmp) / "claude" / "projects" / "project"
        claude_dir.mkdir(parents=True)
        source_path = claude_dir / "source-id.jsonl"
        source_path.write_text('{"type":"user","message":{"role":"user","content":"continue"}}\n')
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [{"name": "claude-one", "short": "one",
            "profile": "work", "config_dir": str(pathlib.Path(self.tmp) / "claude"),
            "cwd": "/tmp/project", "uuid": "source-id"}]}))
        codex_home = pathlib.Path(self.tmp) / "codex"
        target_cwd = pathlib.Path(self.tmp) / "target-worktree"
        target_cwd.mkdir()
        hermes_home = pathlib.Path(self.tmp) / "hermes-home"
        captured = {}
        pane = {"text": "Folder access\nTrust this folder? Codex can read, edit, and run files here.\n"
                         "› 1. Trust and continue\n  2. Back to Agent Command Center\n"
                         "enter continue · esc back"}
        def launch(*args, **kwargs):
            argv = args[0]
            if argv[1] == "capture-pane":
                return mock.Mock(returncode=0, stdout=pane["text"])
            if argv[1] == "send-keys":
                if argv[-1] == "Enter":
                    pane["text"] = "› Ask Codex to do anything\nGPT-6-Luna default\n"
                return mock.Mock(returncode=0, stdout="", stderr="")
            if argv[1] == "kill-session":
                return mock.Mock(returncode=0, stdout="", stderr="")
            # A different fleet operation updates the registry during startup.
            concurrent = json.loads(registry.read_text())
            concurrent["sessions"].append({"name": "unrelated", "desired": "parked"})
            registry.write_text(json.dumps(concurrent))
            captured["argv"] = argv
            self.assertLess(len(os.fsencode(argv[-1])), 1024, "tmux IPC must not carry the excerpt")
            launch_script = pathlib.Path(shlex.split(argv[-1])[-1])
            launch_args = shlex.split(launch_script.read_text().removeprefix("#!/bin/sh\nexec "))
            captured["launch_args"] = launch_args
            captured["prompt"] = launch_args[-1]
            target = codex_home / "sessions" / "2026" / "rollout.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            fixture = (ROOT / "tests/fixtures/codex_rollout.jsonl").read_text().replace(
                "/tmp/project", str(target_cwd))
            ack = "FLEET_HANDOFF_CONTEXT_READ"
            fixture += json.dumps({"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [{"type": "output_text", "text": ack}]}}) + "\n"
            target.write_text(fixture)
            os.utime(target, None)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(codex_handoff.subprocess, "run", side_effect=launch))
            ack_check = stack.enter_context(mock.patch.object(
                codex_handoff, "_acknowledged", side_effect=[False, True]))
            prompt_events = stack.enter_context(mock.patch.object(fleet_boot, "record_codex_prompt"))
            stack.enter_context(mock.patch.object(codex_handoff.fleet_state, "record_codex_prompt_event"))
            stack.enter_context(mock.patch.object(codex_handoff.time, "sleep"))
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": str(hermes_home)}))
            entry = codex_handoff.handoff("one", "codex-one", str(registry), str(codex_home),
                                          target_cwd=str(target_cwd), model="gpt-6.1-sol")
        self.assertEqual([kwargs.get("event", "observed") for _, kwargs in prompt_events.call_args_list],
                         ["observed", "auto-resolved", "cleared"])
        self.assertEqual(ack_check.call_count, 2)
        self.assertIn({"name": "unrelated", "desired": "parked"}, json.loads(registry.read_text())["sessions"])
        saved = json.loads(registry.read_text())["sessions"][-1]
        self.assertEqual(entry["provider"], "codex")
        self.assertEqual(saved["profile"], "work")
        self.assertEqual(entry["cwd"], str(target_cwd))
        self.assertEqual(saved["source_provenance"]["transcript"], str(source_path))
        context_file = pathlib.Path(saved["source_provenance"]["handoff_context"])
        self.assertTrue(context_file.exists())
        self.assertEqual(context_file.stat().st_mode & 0o777, 0o600)
        self.assertIn('"source_transcript": "{}"'.format(source_path), captured["prompt"])
        self.assertIn('{"role": "user", "text": "continue"}', captured["prompt"])
        argv = captured["argv"]
        launch_args = captured["launch_args"]
        self.assertNotIn("--add-dir", launch_args)
        self.assertEqual(launch_args[launch_args.index("--cd") + 1], str(target_cwd))
        prompt = captured["prompt"]
        self.assertLess(len(prompt), 5000)
        self.assertTrue(context_file.parent.exists())
        self.assertEqual(saved["source_provenance"]["handoff_context"], str(context_file))
        self.assertTrue(context_file.exists())
        self.assertEqual(saved["uuid"], "01a17998-dd24-7222-9682-dc353dfbae83")

    def test_handoff_rejects_command_over_safe_argument_limit_before_launch(self):
        claude_dir = pathlib.Path(self.tmp) / "claude" / "projects" / "project"
        claude_dir.mkdir(parents=True)
        source_path = claude_dir / "source-id.jsonl"
        source_path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "continue"}}) + "\n")
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [{"name": "claude-one", "short": "one",
            "profile": "work", "config_dir": str(pathlib.Path(self.tmp) / "claude"),
            "cwd": "/tmp/project", "uuid": "source-id"}]}))
        codex_home = pathlib.Path(self.tmp) / "codex"
        target_cwd = pathlib.Path(self.tmp) / "target-worktree"
        target_cwd.mkdir()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(codex_handoff, "MAX_HANDOFF_COMMAND_BYTES", 256))
            stack.enter_context(mock.patch.object(codex_handoff, "_startup_prompt_state", return_value=(None, None, None)))
            run = stack.enter_context(mock.patch.object(codex_handoff.subprocess, "run"))
            stack.enter_context(mock.patch.object(codex_handoff.time, "monotonic", side_effect=[0, 1000]))
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": str(pathlib.Path(self.tmp) / "hermes-home")}))
            with self.assertRaisesRegex(ValueError, "safe command-line size limit"):
                codex_handoff.handoff("one", "codex-one", str(registry), str(codex_home),
                                      target_cwd=str(target_cwd), model="gpt-6.1-sol")
        run.assert_not_called()

    def test_handoff_preserves_unacknowledged_target_and_context_without_registry_change(self):
        claude_dir = pathlib.Path(self.tmp) / "claude" / "projects" / "project"
        claude_dir.mkdir(parents=True)
        (claude_dir / "source-id.jsonl").write_text('{"type":"user","message":{"content":"ctx"}}\n')
        registry = pathlib.Path(self.tmp) / "registry.json"
        original = {"sessions": [{"name": "claude-one", "short": "one", "profile": "work",
            "config_dir": str(pathlib.Path(self.tmp) / "claude"), "cwd": "/tmp/project", "uuid": "source-id"}]}
        registry.write_text(json.dumps(original))
        def launch(*args, **kwargs):
            argv = args[0]
            if argv[1] == "capture-pane":
                return mock.Mock(returncode=1, stdout="")
            if argv[1] == "kill-session":
                return mock.Mock(returncode=1, stderr="can't find session: codex-one")
            target = pathlib.Path(self.tmp) / "codex" / "sessions" / "2026" / "rollout.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "tests/fixtures/codex_rollout.jsonl", target)
            os.utime(target, None)
        with mock.patch.object(codex_handoff.subprocess, "run", side_effect=launch) as run, \
             mock.patch.object(codex_handoff.time, "sleep"), \
             mock.patch.object(codex_handoff.time, "monotonic", side_effect=[0, 0, 1]), \
             mock.patch.dict(os.environ, {"HOME": str(pathlib.Path(self.tmp) / "hermes-home"), "FLEET_CODEX_HANDOFF_TIMEOUT": "0.5"}):
            with self.assertRaisesRegex(codex_handoff.HandoffNeedsInput, "did not acknowledge") as raised:
                codex_handoff.handoff("one", "codex-one", str(registry), str(pathlib.Path(self.tmp) / "codex"), model="gpt-6.1-sol")
        self.assertEqual(json.loads(registry.read_text()), original)
        self.assertFalse(any("kill-session" in call.args[0] for call in run.call_args_list))
        pending = pathlib.Path(raised.exception.pending_manifest)
        self.assertEqual(json.loads(pending.read_text())["state"], "UNKNOWN")
        self.assertTrue(pathlib.Path(json.loads(pending.read_text())["source_provenance"]["handoff_context"]).exists())

    def test_handoff_preserves_target_and_context_for_owner_prompt(self):
        claude_dir = pathlib.Path(self.tmp) / "claude" / "projects" / "project"
        claude_dir.mkdir(parents=True)
        source_path = claude_dir / "source-id.jsonl"
        source_path.write_text('{"type":"user","message":{"content":"ctx"}}\n')
        registry = pathlib.Path(self.tmp) / "registry.json"
        original = {"sessions": [{"name": "claude-one", "short": "one", "profile": "work",
            "config_dir": str(pathlib.Path(self.tmp) / "claude"), "cwd": "/tmp/project", "uuid": "source-id"}]}
        registry.write_text(json.dumps(original))
        codex_home = pathlib.Path(self.tmp) / "codex"
        codex_home.mkdir()
        hermes_home = pathlib.Path(self.tmp) / "hermes-home"
        calls = []

        def launch(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "capture-pane":
                return mock.Mock(returncode=0, stdout="Hooks need review\\nContinue without trusting")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(codex_handoff.subprocess, "run", side_effect=launch),
            mock.patch.object(codex_handoff.time, "sleep"),
            mock.patch.object(fleet_state, "record_codex_prompt_event") as prompt_event,
            mock.patch.dict(os.environ, {"HOME": str(hermes_home)}),
        ):
            with self.assertRaises(codex_handoff.HandoffNeedsInput) as raised:
                codex_handoff.handoff("one", "codex-one", str(registry), str(codex_home), model="gpt-6.1-sol")

        error = raised.exception
        self.assertEqual(error.status, "NEEDS-INPUT")
        self.assertEqual(prompt_event.call_count, 1)
        self.assertEqual(prompt_event.call_args.args[1]["category"], "hook-review")
        self.assertEqual(json.loads(registry.read_text()), original)
        self.assertFalse(any("send-keys" in call for call in calls))
        self.assertFalse(any("kill-session" in call for call in calls))
        pending = pathlib.Path(error.pending_manifest)
        self.assertTrue(pending.is_file())
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        manifest = json.loads(pending.read_text())
        self.assertEqual(manifest["target_name"], "codex-one")
        self.assertEqual(manifest["source_provenance"]["transcript"], str(source_path))
        self.assertTrue(pathlib.Path(manifest["source_provenance"]["handoff_context"]).is_file())

    def test_handoff_rejects_existing_target_before_launch(self):
        registry = pathlib.Path(self.tmp) / "registry.json"
        registry.write_text(json.dumps({"sessions": [{"name": "codex-one"}]}))
        with mock.patch.object(codex_handoff.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "already registered"):
                codex_handoff.handoff("missing", "codex-one", str(registry), self.tmp, model="gpt-6.1-sol")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
