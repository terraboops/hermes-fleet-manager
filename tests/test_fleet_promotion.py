import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_reg


class Promotion(unittest.TestCase):
    def test_tilde_cwd_expands_target_but_keeps_exact_source_for_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); (p/'project').mkdir()
            context = p/'context'; context.write_text('context')
            transcript = p/'source'; transcript.write_text('history')
            source = dict(name='x', short='x', profile='work', cwd='~/project', uuid='source', desired='parked')
            target = dict(name='candidate', profile='work', provider='codex', uuid='target',
                          source_provenance=dict(session_id='source', transcript=str(transcript), handoff_context=str(context)))
            reg = p/'registry.json'; reg.write_text(json.dumps({'sessions':[source]}))
            with mock.patch.dict(os.environ, HOME=td), mock.patch.object(fleet_reg,'REG',str(reg)), mock.patch.object(fleet_reg,'_probe_live',return_value=False), mock.patch.object(fleet_reg,'transcript_path',return_value=str(transcript)):
                entry = fleet_reg.promote('x',target,'source',str(p/'transaction'))
                self.assertEqual(entry['cwd'],str(p/'project'))
                manifest=p/'transaction/promotion.json'
                self.assertEqual(json.loads(manifest.read_text())['source'],source)
                fleet_reg.rollback_promotion(str(manifest))
                self.assertEqual(fleet_reg.load()['sessions'],[source])

    def test_promote_preserves_name_cwd_and_rollback_without_unparking(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            context = p / 'context.jsonl'; context.write_text('context')
            transcript = p / 'source.jsonl'; transcript.write_text('history')
            source = dict(name='cc-w-x', short='x', profile='work', cwd=td,
                          uuid='source-id', desired='parked')
            target = dict(name='cc-w-x-canary', short='x-canary', profile='work',
                          provider='codex', cwd=td+'/copy', uuid='target-id',
                          source_provenance=dict(provider='claude', session_id='source-id',
                                                 transcript=str(transcript), handoff_context=str(context)))
            reg = p / 'registry.json'; reg.write_text(json.dumps({'sessions':[source, {'name':'other'}]}))
            with mock.patch.object(fleet_reg, 'REG', str(reg)), mock.patch.object(fleet_reg, '_probe_live', return_value=False), mock.patch.object(fleet_reg, 'transcript_path', return_value=str(transcript)):
                self.assertTrue(hasattr(fleet_reg, 'promote'), 'missing scoped promotion protocol')
                entry = fleet_reg.promote('cc-w-x', target, 'source-id', str(p/'transaction'))
                self.assertEqual(entry['name'], source['name'])
                self.assertEqual(entry['cwd'], source['cwd'])
                self.assertEqual(entry['desired'], 'parked')
                self.assertEqual(entry['uuid'], 'target-id')
                self.assertEqual(fleet_reg.load()['sessions'][1], {'name':'other'})
                record = json.loads((p/'transaction'/'promotion.json').read_text())
                self.assertEqual(record['source'], source)
                self.assertEqual(Path(entry['source_provenance']['handoff_context']).read_text(), 'context')
                self.assertTrue(hasattr(fleet_reg, 'rollback_promotion'), 'missing scoped rollback')
                fleet_reg.rollback_promotion(str(p/'transaction'/'promotion.json'))
                self.assertEqual(fleet_reg.load()['sessions'], [source, {'name':'other'}])
