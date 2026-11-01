"""Sanity checks: count bounds, sentinels, coverage against known water."""
import argparse
import json
import os
import tempfile
import unittest

from tests import helpers
from pipeline import packfmt, sanity

T = helpers.T


class CountTest(unittest.TestCase):
    def test_bounds(self):
        self.assertTrue(sanity.count_ok(1000, 900))
        self.assertFalse(sanity.count_ok(1000, 899))
        self.assertTrue(sanity.count_ok(1000, 1250))
        self.assertFalse(sanity.count_ok(1000, 1251))
        self.assertTrue(sanity.count_ok(20, 30))          # small: +-10
        self.assertFalse(sanity.count_ok(20, 31))
        self.assertTrue(sanity.count_ok(None, 5) and sanity.count_ok(0, 5))

    def test_against_previous(self):
        report = {'regions': {'dc': {'places': 100, 'lit_ways': 5000}}}
        prev = {'regions': [{'name': 'dc', 'places': 113, 'lit_ways': 5896}],
                'sources': [{'id': 'us/district-of-columbia', 'counts': {'places': 113, 'lit_ways': 5896}}]}
        srcs = {'us/district-of-columbia': {'counts': {'places': 113, 'lit_ways': 4000}}}
        errs, _ = sanity.check_counts(report, srcs, prev)
        self.assertEqual(len(errs), 3, errs)
        errs, notes = sanity.check_counts(report, srcs, None)
        self.assertEqual(errs, [])
        self.assertTrue(notes)


class SentinelTest(unittest.TestCase):
    def test_sentinels(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'places.pack')
            w = packfmt.PackWriter(p, packfmt.KIND_PLACES, 1, 2)
            els = [{'type': 'way', 'id': 7, 'center': {'lat': 0.21, 'lon': 0.21},
                    'tags': {'amenity': 'hospital', 'name': 'General Hospital'}}]
            w.add_tile(10, 10, packfmt.encode_places_tile(els), 1)
            w.close([], [(10, [(10, 11)])])
            pk = packfmt.Pack(p)
            cfg = {'radius_m': 1500, 'sentinels': [
                {'region': 'x', 'type': 'way', 'id': 7, 'amenity': 'hospital', 'name': 'G', 'lat': 0.21, 'lng': 0.21, 'required': True},
                {'region': 'x', 'type': 'way', 'id': 8, 'amenity': 'hospital', 'name': 'Gone', 'lat': 0.21, 'lng': 0.21, 'required': True},
                {'region': 'x', 'amenity': 'hospital', 'name_re': 'general', 'lat': 0.215, 'lng': 0.215, 'required': False},
                {'region': 'x', 'amenity': 'police', 'name_re': 'general', 'lat': 0.215, 'lng': 0.215, 'required': False},
                {'region': 'y', 'type': 'node', 'id': 1, 'amenity': 'police', 'name': 'Far', 'lat': 5.0, 'lng': 5.0, 'required': True},
            ]}
            errs, warns, n = sanity.check_sentinels(pk, cfg, partial=False)
            self.assertEqual(len(errs), 2, errs)                 # id 8 missing; 'Far' tile not complete
            self.assertEqual(len(warns), 1, warns)               # the police name match
            errs, warns, n = sanity.check_sentinels(pk, cfg, partial=True)
            self.assertEqual(len(errs), 1)                       # the incomplete tile is skipped when partial
            self.assertEqual(n, 4)


if __name__ == '__main__':
    unittest.main()
