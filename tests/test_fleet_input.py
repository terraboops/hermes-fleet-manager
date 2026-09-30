#!/usr/bin/env python3
"""Tests for fleet_input's composer reader.

Two failures these exist to stop, both observed live on 2026-09-26/27:

1. A composer whose TOP border carries a label ("───── maintenance ─────") defeated the
   "row is one repeated glyph" border test. The box was never located, so parse_input fell
   back to reporting the cursor row alone and read_input then handed detect_dim the WHOLE
   pane -- which answered "not dim" about some scrollback row. An empty composer holding
   Claude's dim ghost suggestion was therefore reported as a REAL unsent draft, and the
   overwatch told the operator one of her own messages was sitting unsent.

2. When no box can be located at all, the honest answer is UNKNOWN, not "not dim": a
   confident False is indistinguishable from a genuine draft and invites the same story.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_input  # noqa: E402


BORDER = '─' * 70
LABELLED = BORDER + ' maintenance ─'
DIM = '\x1b[2m'


def esc(text):
    return '\x1b[39m' + text + '\x1b[0m'


class BorderDetection(unittest.TestCase):
    def test_labelled_top_border_is_found(self):
        rows = [LABELLED, '❯\xa0push incubator', BORDER]
        info = fleet_input.parse_input(rows, 1)
        self.assertTrue(info['box_found'], 'a labelled top border is still a border')
        self.assertEqual(info['text'], 'push incubator')
        self.assertEqual((info['top'], info['bottom']), (0, 2))

    def test_unlabelled_borders_still_found(self):
        rows = [BORDER, '❯\xa0do the thing', BORDER]
        info = fleet_input.parse_input(rows, 1)
        self.assertTrue(info['box_found'])
        self.assertEqual(info['text'], 'do the thing')

    def test_menu_is_not_input(self):
        rows = [LABELLED, '❯ 1. Yes', '  2. No', BORDER]
        info = fleet_input.parse_input(rows, 1)
        self.assertTrue(info['menu'])

    def test_status_bar_is_not_a_border(self):
        # A progress bar is mostly not border glyphs; it must not be mistaken for an edge.
        rows = [LABELLED, '❯\xa0draft', BORDER, '   [━━────────] 20%   ▲ 196.6k ▼ 2.7k']
        info = fleet_input.parse_input(rows, 1)
        self.assertEqual(info['bottom'], 2)


class GhostText(unittest.TestCase):
    def test_dim_composer_reads_as_ghost(self):
        rows = [BORDER, 'placeholder', BORDER]
        esc_rows = [BORDER, esc('❯ ' + DIM + 'push incubator'), BORDER]
        dim = fleet_input.detect_dim(esc_rows, 0, 2)
        self.assertTrue(dim)
        self.assertTrue(dim and not fleet_input.parse_input(rows, 1)['menu'])

    def test_typed_text_is_not_dim(self):
        esc_rows = [BORDER, esc('❯ push incubator'), BORDER]
        self.assertFalse(fleet_input.detect_dim(esc_rows, 0, 2))


class NoBoxIsUnknown(unittest.TestCase):
    def test_unlocatable_box_reports_unknown_dim(self):
        # A pane with no borders at all: the region cannot be identified, so dim/ghost are
        # reported as None rather than a False that reads as "a person typed this".
        rows = ['some scrollback line', '❯ maybe a draft or maybe not', '']
        info = fleet_input.parse_input(rows, 1)
        self.assertFalse(info['box_found'])


class DraftStash(unittest.TestCase):
    """The stash/restore pair must not read a phantom occupant from an unread composer.

    Observed live 2026-09-29: with no located box the reader reported the pane's cursor row
    ("❯") as text, so the dispatch stashed a glyph, the restore saw a "non-empty" composer
    and declined to put the operator's real draft back, and three consecutive dispatches
    logged "❯" as the preserved draft.
    """

    def test_no_box_is_not_a_draft(self):
        text, note = fleet_input.draft_text({'box_found': False, 'text': '❯'})
        self.assertEqual(text, '')
        self.assertIn('cursor row', note)

    def test_prompt_glyph_alone_is_not_a_draft(self):
        text, note = fleet_input.draft_text({'box_found': True, 'text': '❯\xa0'})
        self.assertEqual(text, '')
        self.assertEqual(note, 'prompt glyph only')

    def test_real_draft_still_stashed(self):
        text, note = fleet_input.draft_text(
            {'box_found': True, 'text': 'how much space does QMK leave on the flash chip'})
        self.assertEqual(text, 'how much space does QMK leave on the flash chip')
        self.assertIsNone(note)

    def test_unread_composer_is_never_cleared(self):
        # Ctrl-C into a pane whose composer was never located can cancel a prompt or end
        # the session, so the clear refuses rather than trusting the cursor row.
        ok, why = fleet_input.clear_decision({'box_found': False, 'text': '❯', 'empty': False})
        self.assertFalse(ok)
        self.assertIn('not located', why)

    def test_read_composer_with_text_is_cleared(self):
        ok, why = fleet_input.clear_decision(
            {'box_found': True, 'text': 'stale draft', 'empty': False})
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
