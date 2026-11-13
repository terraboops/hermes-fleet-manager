import json
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_reg


def test_codex_to_claude_promotion_and_scoped_rollback(tmp_path):
    source_file = tmp_path / 'source.jsonl'; source_file.write_text('codex history')
    context = tmp_path / 'context.jsonl'; context.write_text('context')
    native = tmp_path / 'claude.jsonl'; native.write_text('claude history')
    source = dict(name='stable', short='s', profile='work', provider='codex', cwd=str(tmp_path), uuid='old', desired='parked')
    target = dict(name='candidate', profile='work', provider='claude', uuid='new', source_provenance=dict(provider='codex', session_id='old', transcript=str(source_file), handoff_context=str(context)))
    registry = tmp_path / 'registry.json'; registry.write_text(json.dumps({'sessions': [source, {'name': 'untouched'}]}))
    with mock.patch.object(fleet_reg, 'REG', str(registry)), mock.patch.object(fleet_reg, '_probe_live', return_value=False), mock.patch.object(fleet_reg, 'transcript_path', return_value=str(native)):
        result = fleet_reg.promote('stable', target, 'old', str(tmp_path / 'transaction'))
        assert result['provider'] == 'claude' and result['desired'] == 'parked'
        assert Path(result['source_provenance']['handoff_context']).read_text() == 'context'
        assert (tmp_path / 'transaction/source-transcript.jsonl').read_text() == 'codex history'
        fleet_reg.rollback_promotion(str(tmp_path / 'transaction/promotion.json'))
        assert fleet_reg.load()['sessions'] == [source, {'name': 'untouched'}]
