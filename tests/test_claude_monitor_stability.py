import types
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from unittest import mock
import fleet_state as state


def test_claude_monitor_is_stable_as_clock_advances():
    with mock.patch.object(state.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
         mock.patch.object(state, 'transcript_entry', return_value={'provider': 'claude'}), \
         mock.patch.object(state, 'sh', return_value='Ready\n$\n'), \
         mock.patch.object(state, 'transcript_of', return_value='/native'), \
         mock.patch.object(state, 'written_recently', return_value=False), \
         mock.patch.object(state, 'event_age', return_value=None), \
         mock.patch.object(state, '_last_output_line', return_value=None), \
         mock.patch.object(state.fleet_transcript, 'newest_inbound', return_value=(100, 'Read README')), \
         mock.patch.object(state.time, 'time', side_effect=[1000, 1005]):
        assert state.fingerprint('candidate') == state.fingerprint('candidate')
