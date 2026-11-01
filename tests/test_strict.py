"""The strict area and the reviewed-empty tiles (pipeline/strict.py, pack.py, sanity.check_strict)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import helpers
from pipeline import extract, pack, packfmt, regions, sanity, store, strict

T = helpers.T
ROOT = helpers.ROOT


def at(fx, fy):
    return round(fx * T), round(fy * T)


class ReviewedEmptyTest(unittest.TestCase):
    """pack.py adds a reviewed tile only while no element touches it."""

    def test_added_already_and_contradicted(self):
        with tempfile.TemporaryDirectory() as d:
            pbf = os.path.join(d, 'a.osm.pbf')
            helpers.write_osm(pbf, [
                (1, *at(10.5, 10.5), {'amenity': 'police'}),
                (2, *at(12.2, 13.2), {}), (3, *at(10.5, 12.5), {}), (4, *at(11.5, 12.5), {}),
            ], [
                (10, [2, 999], {'amenity': 'pharmacy'}),                       # node 999 missing: (12,13) incomplete
                (11, [3, 4], {'highway': 'footway', 'lit': 'yes'}),
            ])
            index = os.path.join(d, 'index.json')
            helpers.write_index(index, {'s/a': helpers.square(8 * T - 9, 8 * T - 9, 16 * T + 9, 16 * T + 9)})
            reg = os.path.join(d, 'regions.json')
            helpers.write_regions(reg, [{'name': 'r', 'box': [helpers.deg(10 * T + 5), helpers.deg(10 * T + 9),
                                                              helpers.deg(10 * T + 5), helpers.deg(10 * T + 9)],
                                         'margin_km': 0}])
            rev = os.path.join(d, 'reviewed.json')
            with open(rev, 'w') as f:
                json.dump({'groups': [{'name': 'sea', 'why': 'open water', 'reviewed': 'test',
                                       'places': {'tiles': ['20,20', '14,14', '10,10', '12,13'],
                                                  'rects': [{'x': [30, 31], 'y': [5, 5]}]},
                                       'lighting': {'tiles': ['20,20', '14,14', '11,12']}}]}, f)
            sp = os.path.join(d, 's.sqlite')
            db = store.open_store(sp)
            extract.extract(pbf, 's/a', db, 'whole')
            db.close()
            out = os.path.join(d, 'out')
            rep = pack.build(sp, index, reg, out, reviewed_path=rev)
            r = rep['reviewed_empty']
            self.assertEqual(r['places']['reviewed'], 6)
            self.assertEqual(r['places']['added'], 3)                         # 20,20 and 30,5 and 31,5
            self.assertEqual(r['places']['already_complete'], 1)              # 14,14: complete, empty
            # Data in a reviewed tile contradicts the review, complete or not.
            self.assertEqual(r['places']['contradicted'], ['10,10 (sea)', '12,13 (sea)'])
            self.assertEqual(r['lighting']['added'], 1)
            self.assertEqual(r['lighting']['already_complete'], 1)
            self.assertEqual(r['lighting']['contradicted'], ['11,12 (sea)'])
            places = packfmt.Pack(os.path.join(out, 'places.pack'))
            self.assertTrue(places.complete(20, 20) and places.complete(31, 5))
            self.assertEqual(places.places(20, 20), [])
            self.assertFalse(places.complete(12, 13), 'contradicted: stays incomplete')
            self.assertTrue(packfmt.Pack(os.path.join(out, 'lighting.pack')).complete(20, 20))
            # Without the file nothing is added.
            rep0 = pack.build(sp, index, reg, os.path.join(d, 'out0'))
            self.assertEqual(rep0['reviewed_empty']['places']['added'], 0)
            self.assertFalse(packfmt.Pack(os.path.join(d, 'out0', 'places.pack')).complete(20, 20))

    def test_a_group_must_say_why_and_who(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'r.json')
            with open(p, 'w') as f:
                json.dump({'groups': [{'name': 'x', 'places': {'tiles': ['1,1']}}]}, f)
            with self.assertRaisesRegex(ValueError, 'why'):
                strict.load_reviewed(p)

    def test_the_committed_review_file_loads(self):
        r = strict.load_reviewed(os.path.join(ROOT, 'config', 'reviewed_empty.json'))
        self.assertEqual(len(r['places']), 45 + 272)
        self.assertEqual(len(r['lighting']), 102)
        self.assertEqual(r['places'][(-4377, 2088)], 'lake-michigan-chicago')


def _pack(path, kind, tiles_with_data, runs):
    w = packfmt.PackWriter(path, kind, 1, 2)
    strings = ['yes'] if kind == packfmt.KIND_LIGHTING else []
    for tx, ty in sorted(tiles_with_data, key=lambda t: (t[1], t[0])):
        if kind == packfmt.KIND_LIGHTING:
            x0, y0 = tx * T, ty * T
            payload = packfmt.encode_lighting_tile(tx, ty, [(7, 0, [x0 + 5, y0 + 5, x0 + 9, y0 + 9])], T)
        else:
            payload = packfmt.encode_places_tile([{'type': 'node', 'id': 1, 'lat': 0, 'lon': 0, 'tags': {}}])
        w.add_tile(tx, ty, payload, 1)
    w.close(strings, runs)
    return packfmt.Pack(path)


class StrictCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # One region: tiles x 10..12, y 20..21.
        self.cfg = {'regions': [{'name': 'r', 'box': [helpers.deg(20 * T + 5), helpers.deg(21 * T + 5),
                                                      helpers.deg(10 * T + 5), helpers.deg(12 * T + 5)],
                                 'margin_km': 0}]}
        self.area = strict.strict_tiles(self.cfg, None)

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return os.path.join(self.tmp.name, name)

    def test_the_area_is_the_box_tiles(self):
        self.assertEqual(self.area, {(x, y) for x in (10, 11, 12) for y in (20, 21)})

    def test_complete_area_passes(self):
        places = _pack(self.p('p'), packfmt.KIND_PLACES, [], [(20, [(10, 12)]), (21, [(10, 12)])])
        light = _pack(self.p('l'), packfmt.KIND_LIGHTING, [(11, 20)], [(20, [(10, 12)]), (21, [(10, 12)])])
        errs, info = sanity.check_strict({}, places, light, None, False, self.cfg)
        self.assertEqual(errs, [])
        self.assertEqual(info['tiles'], 6)

    def test_a_places_hole_fails(self):
        places = _pack(self.p('p'), packfmt.KIND_PLACES, [], [(20, [(10, 12)]), (21, [(10, 11)])])
        light = _pack(self.p('l'), packfmt.KIND_LIGHTING, [], [(20, [(10, 12)]), (21, [(10, 12)])])
        errs, _ = sanity.check_strict({}, places, light, None, False, self.cfg)
        self.assertEqual(len(errs), 1)
        self.assertIn("'12,21'", errs[0])

    def test_a_lighting_hole_next_to_lit_streets_fails_one_far_away_does_not(self):
        places = _pack(self.p('p'), packfmt.KIND_PLACES, [], [(20, [(10, 12)]), (21, [(10, 12)])])
        light = _pack(self.p('l'), packfmt.KIND_LIGHTING, [(10, 20)], [(20, [(10, 11)]), (21, [(10, 12)])])
        errs, _ = sanity.check_strict({}, places, light, None, False, self.cfg)
        self.assertEqual(len(errs), 1)
        self.assertIn('12,20', errs[0])
        light2 = _pack(self.p('l2'), packfmt.KIND_LIGHTING, [(30, 30)], [(20, [(10, 11)]), (21, [(10, 12)]),
                                                                        (30, [(30, 30)])])
        errs, _ = sanity.check_strict({}, places, light2, None, False, self.cfg)
        self.assertEqual(errs, [])

    def test_contradicted_review_fails(self):
        places = _pack(self.p('p'), packfmt.KIND_PLACES, [], [(20, [(10, 12)]), (21, [(10, 12)])])
        light = _pack(self.p('l'), packfmt.KIND_LIGHTING, [], [(20, [(10, 12)]), (21, [(10, 12)])])
        rep = {'reviewed_empty': {'places': {'contradicted': ['1,1 (sea)']}, 'lighting': {'contradicted': []}}}
        errs, _ = sanity.check_strict(rep, places, light, None, False, self.cfg)
        self.assertEqual(len(errs), 1)
        self.assertIn('reviewed as empty have data', errs[0])


class StrictAreaFileTest(unittest.TestCase):
    """tools/strict_area.py: the file the backend commits."""

    def test_rows_hold_exactly_the_strict_tiles(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'strict.json')
            subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'tools', 'strict_area.py'), out],
                           check=True, capture_output=True)
            with open(out) as f:
                doc = json.load(f)
            got = {(x, y) for y, runs in doc['rows'] for x0, x1 in runs for x in range(x0, x1 + 1)}
            cfg = regions.load(os.path.join(ROOT, 'config', 'regions.json'))
            want = strict.strict_tiles(cfg, os.path.join(ROOT, 'config', 'uk_outline.json'))
            self.assertEqual(got, want)
            self.assertEqual(doc['tiles'], len(want))
            # London, Edinburgh, Douglas (Isle of Man), St Helier (Jersey), Belfast, Times Square.
            for lat, lng in ((51.5074, -0.1278), (55.9533, -3.1883), (54.1523, -4.4861), (49.1868, -2.1069),
                             (54.5973, -5.9301), (40.758, -73.9855)):
                self.assertIn((int(lng // 0.02), int(lat // 0.02)), got, (lat, lng))
            # Dublin, Paris, Miami are not.
            for lat, lng in ((53.3498, -6.2603), (48.8566, 2.3522), (25.7617, -80.1918)):
                self.assertNotIn((int(lng // 0.02), int(lat // 0.02)), got, (lat, lng))


if __name__ == '__main__':
    unittest.main()
