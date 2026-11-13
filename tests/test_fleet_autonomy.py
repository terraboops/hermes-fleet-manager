import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_autonomy as fa

# Grants are private config; the tests carry their own so they never read the real file.
fa.GRANTS = fa.load_grants(str(Path(__file__).resolve().parents[1] / 'scripts/grants.example.json'))

OLD = ['--model', 'sonnet', '--safe-mode', '--restricted', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
       '--permission-mode', 'auto', '--tools', 'Bash,Read']


def settings(args):
    return json.loads(args[args.index('--settings') + 1])['autoMode']


class AutonomyTest(unittest.TestCase):
    def test_grant_drops_restrictions_keeps_the_rest_and_defaults_first(self):
        new = fa.grant_args(OLD, 'example')
        self.assertNotIn('--safe-mode', new)
        self.assertNotIn('--restricted', new)
        self.assertEqual(new[:2], ['--model', 'sonnet'])
        self.assertIn('--permission-mode', new)
        self.assertIn('Bash,Read', new)
        block = settings(new)
        self.assertEqual(list(block), ['allow'])  # never writes hard_deny, soft_deny or environment
        self.assertEqual(block['allow'][0], '$defaults')
        self.assertEqual(len(block['allow']), 1 + len(fa.GRANTS['example']))

    def test_grant_is_idempotent(self):
        once = fa.grant_args(OLD, 'example')
        self.assertEqual(fa.grant_args(once, 'example'), once)
        self.assertEqual(once.count('--settings'), 1)

    def test_other_settings_pairs_survive(self):
        other = json.dumps({'env': {'X': '1'}})
        new = fa.grant_args(OLD + ['--settings', other], 'example')
        self.assertEqual(new.count('--settings'), 2)
        self.assertIn(other, new)

    def test_move_refuses_restrictions_the_source_never_had(self):
        with self.assertRaisesRegex(ValueError, 'never had'):
            fa.check_unowned_restrictions({'args': []}, OLD)
        fa.check_unowned_restrictions({'args': []}, OLD, allow_restricted=True)
        fa.check_unowned_restrictions({'args': ['--restricted']}, OLD)
        fa.check_unowned_restrictions({'args': []}, fa.grant_args(OLD, 'example'))

    def test_carry_over_keeps_source_grants_and_strips_unowned_restrictions(self):
        src = {'args': ['--settings', json.dumps({'autoMode': {'allow': ['$defaults', 'G1']}})],
               'auto_mode_allow': ['G2']}
        out = fa.carry_over(src, OLD)
        self.assertNotIn('--restricted', out)
        self.assertNotIn('--safe-mode', out)
        self.assertEqual(settings(out)['allow'], ['$defaults', 'G1', 'G2'])

    def test_carry_over_keeps_restrictions_when_source_had_them(self):
        self.assertIn('--restricted', fa.carry_over({'args': ['--safe-mode', '--restricted']}, OLD))

    def test_unknown_grant_rejected(self):
        with self.assertRaises(ValueError):
            fa.grant_args(OLD, 'nope')


class PolicyChangeGateTest(unittest.TestCase):
    def test_without_auto_mode_restores_the_pre_grant_launch(self):
        granted = fa.grant_args(OLD, 'example')
        self.assertEqual(fa.without_auto_mode(granted), fa.strip_restrictions(OLD))


if __name__ == '__main__':
    unittest.main()
