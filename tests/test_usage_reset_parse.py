"""The usage screen writes a calendar date ("Oct 26 at 12am"); it must parse to that exact moment."""
import datetime
import unittest

import usage_meter


class ResetParse(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 21, 13, 0).timestamp()

    def _at(self, text):
        return datetime.datetime.fromtimestamp(usage_meter.parse_reset(text, self.NOW))

    def test_screen_format_date(self):
        self.assertEqual(self._at("Oct 26 at 12am"), datetime.datetime(2026, 10, 26, 0, 0))
        self.assertEqual(self._at("Oct 23 at 2am"), datetime.datetime(2026, 10, 23, 2, 0))
        self.assertEqual(self._at("Oct 21 at 5pm"), datetime.datetime(2026, 10, 21, 17, 0))

    def test_noon_and_midnight(self):
        self.assertEqual(self._at("Oct 27 at 12pm"), datetime.datetime(2026, 10, 27, 12, 0))

    def test_a_past_date_rolls_to_next_year(self):
        self.assertEqual(self._at("Oct 20 at 2am"), datetime.datetime(2027, 10, 20, 2, 0))

    def test_older_formats_still_work(self):
        self.assertEqual(self._at("11:00 PM"), datetime.datetime(2026, 10, 21, 23, 0))
        self.assertEqual(self._at("Fri 2:00 AM"), datetime.datetime(2026, 10, 23, 2, 0))


if __name__ == "__main__":
    unittest.main()
