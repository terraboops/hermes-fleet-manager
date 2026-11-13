import json
import sys
from pathlib import Path
from datetime import datetime, timezone
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_handoff as h


def bundle(tmp_path):
    source = Path(__file__).parent / 'fixtures/codex_rollout.jsonl'
    record = h.prepare(source, 'native-id', tmp_path / 'bundle', 'Await owner approval; no implementation')
    doc = tmp_path / 'README.md'
    doc.write_text('Local project fact')
    row = dict(name='source', short='s', uuid='native-id', provider='codex', profile='work', cwd=str(tmp_path), desired='up')
    return h.plan_candidate(record, row, 'reverse-pilot', 'opus', str(doc), ['Await owner approval'])


def test_plan_is_private_explicit_read_only_and_preserves_source(tmp_path):
    plan = bundle(tmp_path)
    target = plan['target']
    assert target['provider'] == 'claude' and target['desired'] == 'parked'
    assert target['config_dir'] == str(Path('~/.claude-work').expanduser())
    assert target['uuid'] != 'native-id'
    assert '--safe-mode' in target['args'] and '--permission-mode' in target['args']
    assert '--remote-control' in target['args']
    assert plan['source']['desired'] == 'up'
    assert Path(plan['prompt_file']).stat().st_mode & 0o777 == 0o600
    assert 'Await owner approval' not in Path(plan['prompt_file']).read_text()  # facts must come from context
    assert Path(plan['manifest']).is_file()


def test_plan_rejects_non_array_semantic_expectations(tmp_path):
    source = Path(__file__).parent / 'fixtures/codex_rollout.jsonl'
    record = h.prepare(source, 'id', tmp_path / 'bundle', 'read only')
    doc = tmp_path / 'README.md'
    doc.write_text('fact')
    row = dict(name='source', uuid='id', provider='codex', profile='work', cwd=str(tmp_path))
    with pytest.raises(ValueError, match='facts'):
        h.plan_candidate(record, row, 'candidate', 'opus', str(doc), 'notanarray')


def native(plan):
    t = plan['target']
    records = []
    paths = [t['source_provenance']['handoff_context'], t['source_provenance']['instruction_file'], plan['document']]
    for i, path in enumerate(paths + ['pwd']):
        tool = dict(type='tool_use', id=str(i), name='Read' if path != 'pwd' else 'Bash',
                    input={'file_path': path} if path != 'pwd' else {'command': 'pwd'})
        records.append(dict(type='assistant', sessionId=t['uuid'], cwd=t['cwd'], message={'content': [tool]}))
        records.append(dict(type='user', sessionId=t['uuid'], cwd=t['cwd'], message={'content': [
            dict(type='tool_result', tool_use_id=str(i), content=t['cwd'] if path == 'pwd' else 'read contents')]}))
    records.append(dict(type='assistant', sessionId=t['uuid'], cwd=t['cwd'], message={
        'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Await owner approval\n' + plan['marker']}]}))
    return records


def test_verify_requires_native_tools_success_and_final_semantics(tmp_path):
    plan = bundle(tmp_path)
    path = tmp_path / 'native.jsonl'
    records = native(plan)
    def save():
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    save()
    evidence = h.verify_native(plan, path)
    assert evidence['uuid'] == plan['target']['uuid']
    records[1]['message']['content'][0]['is_error'] = True
    save()
    with pytest.raises(ValueError, match='reads'):
        h.verify_native(plan, path)
    records = native(plan)
    records[-1]['message']['content'][0]['text'] = plan['marker']
    save()
    with pytest.raises(ValueError, match='semantic'):
        h.verify_native(plan, path)
    records = native(plan)
    records[-1]['sessionId'] = 'wrong'
    save()
    with pytest.raises(ValueError, match='identity'):
        h.verify_native(plan, path)


def test_launch_enforces_reset_duplicate_guard_and_short_private_launcher(tmp_path, monkeypatch):
    import subprocess
    plan = bundle(tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if 'has-session' in args else 0, '', '')
    monkeypatch.setattr(h, 'run_command', run, raising=False)
    with pytest.raises(ValueError, match='reset'):
        h.launch_candidate(plan, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert not calls
    receipt = h.launch_candidate(plan, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert receipt['status'] == 'launched-unverified'
    assert len(calls[-1][-1]) < 1024
    launcher = Path(receipt['launcher'])
    assert launcher.stat().st_mode & 0o777 == 0o600
    assert '--session-id' in launcher.read_text()
    assert '--resume' not in launcher.read_text()
    # Execute the generated launcher with only exec/chdir/clock substituted:
    # no real CLI, tmux session or inference, but real argv/env construction.
    import datetime as dt
    executed = {}
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 31, tzinfo=timezone.utc)
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(h.os, 'chdir', lambda cwd: executed.update(cwd=cwd))
    monkeypatch.setattr(h.os, 'execvpe', lambda cmd, argv, env: executed.update(cmd=cmd, argv=argv, env=env))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-not-a-key')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'fixture-not-a-token')
    exec(compile(launcher.read_text(), str(launcher), 'exec'), {})
    assert executed['argv'][-1] == Path(plan['prompt_file']).read_text()
    assert executed['argv'][-2] == '--'
    assert executed['cwd'] == plan['target']['cwd']
    assert executed['env']['CLAUDE_CONFIG_DIR'] == plan['target']['config_dir']
    assert 'ANTHROPIC_API_KEY' not in executed['env']
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in executed['env']
    with pytest.raises(FileExistsError):
        h.launch_candidate(plan, now=datetime(2026, 10, 31, tzinfo=timezone.utc))


def test_resume_requires_native_success_and_records_fresh_offset(tmp_path, monkeypatch):
    import subprocess
    plan = bundle(tmp_path)
    home = tmp_path / 'claude-home'
    plan['target']['config_dir'] = str(home)
    transcript = home / 'projects/project' / (plan['target']['uuid'] + '.jsonl')
    transcript.parent.mkdir(parents=True)
    transcript.write_text(''.join(json.dumps(r) + '\n' for r in native(plan)))
    monkeypatch.setattr(h, 'run_command', lambda args, **kw: subprocess.CompletedProcess(args, 1 if 'has-session' in args else 0, '', ''), raising=False)
    receipt = h.launch_candidate(plan, resume=True, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert receipt['offset'] == transcript.stat().st_size
    assert '--resume' in Path(receipt['launcher']).read_text()
    with pytest.raises(ValueError):
        h.verify_native(plan, transcript, receipt['offset'])


def test_orchestration_writes_evidence_without_registry_changes(tmp_path, monkeypatch):
    plan = bundle(tmp_path)
    transcript = tmp_path / 'native.jsonl'
    transcript.write_text(''.join(json.dumps(r) + '\n' for r in native(plan)))
    monkeypatch.setattr(h, 'native_path', lambda p: transcript)
    result = h.wait_candidate(plan, timeout=0)
    assert result['mechanical_gate'] == 'passed'
    assert json.loads((tmp_path / 'bundle/launch-verification.json').read_text())['uuid'] == plan['target']['uuid']
    transcript.write_text('{}\n')
    (tmp_path / 'bundle/resume-receipt.json').write_text(json.dumps({'offset': 0}))
    with pytest.raises(TimeoutError, match='preserved'):
        h.wait_candidate(plan, timeout=0, resume=True)


def test_cli_verify_plan_executes_real_parser(tmp_path):
    import subprocess
    plan = bundle(tmp_path)
    home = tmp_path / 'home'
    plan['target']['config_dir'] = str(home)
    path = home / 'projects/p' / (plan['target']['uuid'] + '.jsonl')
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in native(plan)))
    Path(plan['manifest']).write_text(json.dumps(plan))
    result = subprocess.run([sys.executable, h.__file__, '--action', 'verify', '--plan', plan['manifest'], '--timeout', '0'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['mechanical_gate'] == 'passed'


def test_resume_refuses_a_surviving_native_writer(tmp_path, monkeypatch):
    import subprocess
    plan = bundle(tmp_path)
    def run(args, **kw):
        if args[0] == 'ps':
            return subprocess.CompletedProcess(args, 0, '123 claude --resume ' + plan['target']['uuid'], '')
        return subprocess.CompletedProcess(args, 1, '', '')
    monkeypatch.setattr(h, 'run_command', run)
    with pytest.raises(ValueError, match='writer'):
        h.launch_candidate(plan, resume=True, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
