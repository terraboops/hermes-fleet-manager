import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import claude_handoff


def test_prepare_preserves_native_codex_context_and_pending_instruction(tmp_path):
    source = Path(__file__).parent / 'fixtures/codex_rollout.jsonl'
    record = claude_handoff.prepare(source, 'native-id', tmp_path / 'handoff', 'Do not implement; awaiting approval')
    assert record['provider'] == 'codex' and record['session_id'] == 'native-id'
    assert 'awaiting approval' in Path(record['instruction_file']).read_text()
    assert Path(record['handoff_context']).read_text().count('"role"') > 0
    assert Path(record['transcript_snapshot']).read_bytes() == source.read_bytes()
    assert (tmp_path / 'handoff' / 'provenance.json').is_file()
    with pytest.raises(FileExistsError):
        claude_handoff.prepare(source, 'native-id', tmp_path / 'handoff', 'other')
