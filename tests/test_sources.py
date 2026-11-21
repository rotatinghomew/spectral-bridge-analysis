from __future__ import annotations

import shutil
from pathlib import Path
import tempfile
import unittest

from check_sources import load_sources

DATA = Path(__file__).resolve().parents[1] / 'data'


class ExchangeTests(unittest.TestCase):
    def test_example_is_readable(self):
        tables = load_sources(DATA)
        self.assertTrue(all(tables.values()))
        self.assertGreater(len({r['instrument'] for r in tables['readings']}), 3)
        self.assertEqual({r['purpose'] for r in tables['slices']}, {'fit', 'holdout', 'order'})
        self.assertTrue({r['withdraw_swap'] for r in tables['pair_changes']} <= {r['swap'] for r in tables['slices']})
        self.assertTrue(any(r['day'] < r['received'] for r in tables['readings']))

    def test_duplicate_identity_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / 'copies'
            shutil.copytree(DATA, duplicate)
            path = duplicate / 'instruments.csv'
            rows = path.read_text(encoding='utf-8').splitlines()
            path.write_text('\n'.join(rows + [rows[1]]) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '标识为空或重复'):
                load_sources(duplicate)
