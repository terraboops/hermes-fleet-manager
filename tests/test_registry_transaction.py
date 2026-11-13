"""Registry writers must refuse stale snapshots, not lose concurrent rows."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import fleet_reg


class RegistryTransaction(unittest.TestCase):
    def test_boot_writer_cannot_erase_concurrent_registration(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
        import fleet_boot
        with tempfile.TemporaryDirectory() as td, mock.patch.object(fleet_reg, 'REG', td + '/registry.json'), mock.patch.object(fleet_boot, 'REG', td + '/registry.json'):
            pathlib.Path(fleet_reg.REG).write_text(json.dumps({'sessions': []}))
            stale = fleet_boot.load_registry()
            fresh = fleet_reg.load()
            fresh['sessions'].append({'name': 'target'})
            fleet_reg.save(fresh)
            with self.assertRaisesRegex(ValueError, 'changed'):
                fleet_boot.atomic_json(fleet_boot.REG, stale)


    def test_stale_writer_cannot_erase_a_promoted_row(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(fleet_reg, 'REG', td + '/registry.json'):
            pathlib.Path(fleet_reg.REG).write_text(json.dumps({'sessions': [{'name': 'source'}]}))
            stale = fleet_reg.load()
            fresh = fleet_reg.load()
            fresh['sessions'].append({'name': 'target'})
            fleet_reg.save(fresh)
            stale['sessions'][0]['desired'] = 'parked'
            with self.assertRaisesRegex(ValueError, 'changed'):
                fleet_reg.save(stale)
            self.assertEqual([r['name'] for r in fleet_reg.load()['sessions']], ['source', 'target'])
