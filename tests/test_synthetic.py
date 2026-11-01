"""End to end on hand-made OSM files: every selection and geometry rule, with known answers.

The same files also run through golden.py, which must agree with the packer
on file A alone and must notice where the merged build (A + B) differs.
"""
import argparse
import json
import os
import tempfile
import unittest

from tests import helpers
from pipeline import extract, golden, pack, packfmt, store

T = helpers.T


def at(fx, fy):
    """Tile coordinates (fractions of a tile) -> 1e-7 degree integers."""
    return round(fx * T), round(fy * T)


NODES_A = [
    (1, *at(11, 11), {'amenity': 'hospital', 'name': 'Corner Hospital', 'note': 'dropped', 'building': 'yes'}),
    (2, *at(10.5, 10.5), {'amenity': 'pharmacy', 'name': 'Old Pharmacy'}),
    (3, *at(12.5, 10.5), {'shop': 'convenience', 'opening_hours': 'Mo-Su 24/7'}),
    (4, *at(12.5, 10.6), {'shop': 'Convenience', 'opening_hours': '24/7'}),
    (5, *at(12.6, 10.5), {'shop': 'supermarket', 'opening_hours': '24/7; PH off'}),
    (6, *at(12.7, 10.5), {'amenity': 'police', 'shop': 'convenience'}),
    (7, *at(12.8, 10.5), {'amenity': 'fuel', 'shop': 'convenience', 'opening_hours': '24/7'}),
    (8, *at(12.9, 10.5), {'shop': 'convenience', 'opening_hours': 'Mo-Fr 08:00-20:00'}),
    (9, *at(13.2, 10.5), {}),
    (10, *at(15.5, 15.5), {}),
    (11, *at(14.5, 14.5), {'amenity': 'clinic', 'name': 'Not selected'}),
    # way nodes
    (20, *at(10.5, 12.5), {}), (21, *at(12.5, 12.5), {}),
    (22, *at(10.5, 13.5), {}), (23, *at(11.5, 14.5), {}),
    (24, *at(13.2, 12.2), {}), (25, *at(13.8, 12.2), {}), (26, *at(13.8, 12.9), {}), (27, *at(13.2, 12.9), {}),
    (28, *at(14.2, 10.2), {}), (29, *at(14.6, 10.2), {}), (30, *at(14.6, 10.6), {}),
    (31, *at(9.2, 9.2), {}), (32, *at(9.4, 9.2), {}),
    (33, *at(12.2, 13.2), {}),
]
WAYS_A = [
    (100, [20, 21], {'highway': 'footway', 'lit': 'yes'}, 3),             # crosses (11,12) with no node in it
    (101, [22, 23], {'highway': 'residential', 'lit': 'no'}),             # through the corner (11,14)
    (102, [31, 32], {'highway': 'service'}),
    (103, [31, 32], {'lit': 'yes', 'man_made': 'pier'}),
    (104, [24, 25, 26, 27, 24], {'amenity': 'hospital', 'name': 'Way Hospital', 'emergency': 'yes'}),
    (105, [31, 32], {'amenity': 'police', 'highway': 'pedestrian', 'lit': '24/7'}),
    (106, [28, 29, 30, 28], {}),
    (107, [33, 999], {'amenity': 'pharmacy'}),                            # node 999 is not in the file
]
RELS_A = [
    (200, [('w', 106, 'outer'), ('n', 9, '')], {'amenity': 'hospital', 'type': 'multipolygon', 'name': 'Rel Hospital'}),
    (201, [('w', 9999, 'outer'), ('n', 10, '')], {'amenity': 'hospital', 'type': 'site'}),
    (202, [('n', 9, '')], {'amenity': 'police'}),
    (203, [('n', 9, '')], {'shop': 'supermarket', 'opening_hours': '24/7'}),
]
NODES_B = [
    (2, *at(10.5, 10.5), {'amenity': 'pharmacy', 'name': 'Newer Pharmacy', 'opening_hours': '24/7'}, 2),
    (3, *at(12.5, 10.5), {'shop': 'convenience', 'opening_hours': 'Mo-Su 24/7', 'name': 'stale'}, 0),
]


class SyntheticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls.a = os.path.join(d, 'a.osm.pbf')
        cls.b = os.path.join(d, 'b.osm.pbf')
        helpers.write_osm(cls.a, NODES_A, WAYS_A, RELS_A)
        helpers.write_osm(cls.b, NODES_B, timestamp='2026-10-31T21:23:06Z')
        cls.index = os.path.join(d, 'index.json')
        helpers.write_index(cls.index, {'synth/a': helpers.square(8 * T - 999, 8 * T - 999, 16 * T + 999, 16 * T + 999),
                                        'synth/b': helpers.square(10 * T, 10 * T, 11 * T, 11 * T)})
        cls.regions = os.path.join(d, 'regions.json')
        helpers.write_regions(cls.regions, [{'name': 'box', 'box': [helpers.deg(12 * T + 5), helpers.deg(12 * T + 9),
                                                                    helpers.deg(11 * T + 5), helpers.deg(11 * T + 9)],
                                             'margin_km': 0}])
        cls.out_a = cls._build(d, 'only-a', [(cls.a, 'synth/a', 'whole')])
        cls.out_ab = cls._build(d, 'a-and-b', [(cls.a, 'synth/a', 'whole'), (cls.b, 'synth/b', 'whole')])
        cls.out_box = cls._build(d, 'boxes', [(cls.a, 'synth/a', 'boxes')])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _build(cls, d, name, files):
        sp = os.path.join(d, name + '.sqlite')
        db = store.open_store(sp)
        from pipeline import regions
        mask = regions.lighting_mask(regions.load(cls.regions))
        for path, sid, mode in files:
            extract.extract(path, sid, db, mode, mask if mode == 'boxes' else None)
        db.close()
        out = os.path.join(d, name)
        cls.report = pack.build(sp, cls.index, cls.regions, out)
        return out

    def places(self, out):
        return packfmt.Pack(os.path.join(out, 'places.pack'))

    def lighting(self, out):
        return packfmt.Pack(os.path.join(out, 'lighting.pack'))

    def ids(self, els):
        return [f"{e['type']}/{e['id']}" for e in els]

    def test_selection_and_tags(self):
        pk = self.places(self.out_a)
        t = pk.places(12, 10)
        self.assertEqual(self.ids(t), ['node/3', 'node/5', 'node/6', 'node/7'])   # not 4 (case), not 8 (hours)
        corner = [e for e in pk.places(10, 10) if e['id'] == 1][0]
        self.assertEqual(corner, {'type': 'node', 'id': 1, 'lat': helpers.deg(11 * T), 'lon': helpers.deg(11 * T),
                                  'tags': {'amenity': 'hospital', 'name': 'Corner Hospital'}})
        all_ids = {i for k in pk.index for i in self.ids(pk.places(*k))}
        for gone in ('node/4', 'node/8', 'node/11', 'relation/202', 'relation/203', 'way/106'):
            self.assertNotIn(gone, all_ids)
        self.assertIn('node/2', all_ids)                 # every pharmacy, 24/7 or not

    def test_node_on_a_corner_is_in_four_tiles(self):
        pk = self.places(self.out_a)
        holders = sorted(k for k in pk.index if any(e['id'] == 1 and e['type'] == 'node' for e in pk.places(*k)))
        self.assertEqual(holders, [(10, 10), (10, 11), (11, 10), (11, 11)])

    def test_way_center_and_order(self):
        pk = self.places(self.out_a)
        t = pk.places(13, 12)
        self.assertEqual(self.ids(t), ['way/104'])
        self.assertEqual(t[0]['center'], {'lat': helpers.deg(round(12.55 * T)), 'lon': helpers.deg(round(13.5 * T))})
        rel = [e for e in pk.places(14, 10) if e['type'] == 'relation']
        self.assertEqual(self.ids(rel), ['relation/200'])
        # bbox of node 9 (13.2, 10.5) and way 106 (14.2-14.6, 10.2-10.6)
        self.assertEqual(rel[0]['center'], {'lat': helpers.deg(round(10.4 * T)), 'lon': helpers.deg(round(13.9 * T))})
        self.assertEqual(self.ids(pk.places(13, 10)), ['relation/200'])

    def test_missing_members_and_nodes_make_tiles_incomplete(self):
        pk = self.places(self.out_a)
        self.assertFalse(pk.complete(15, 15))            # relation 201's known member
        self.assertFalse(pk.complete(12, 13))            # way 107 lost node 999
        self.assertTrue(pk.complete(14, 14) and pk.complete(8, 8) and pk.complete(15, 8))
        self.assertFalse(pk.complete(16, 8))             # outside the polygon

    def test_lighting(self):
        lk = self.lighting(self.out_a)
        self.assertEqual([w[0] for w in lk.lighting(11, 12)], [100])
        for t in [(10, 13), (11, 13), (10, 14), (11, 14)]:
            self.assertEqual([(w[0], w[1]) for w in lk.lighting(*t)], [(101, 'no')], t)
        self.assertEqual([(w[0], w[1]) for w in lk.lighting(9, 9)], [(105, '24/7')])
        w = lk.lighting(12, 12)[0]
        self.assertEqual(w, (100, 'yes', [*at(10.5, 12.5), *at(12.5, 12.5)]))

    def test_boxes_mode_clips_lighting_only(self):
        lk = self.lighting(self.out_box)
        complete = {(x, y) for y, runs in lk.coverage.items() for a, b in runs for x in range(a, b + 1)}
        self.assertEqual(complete, {(11, 12)})          # the one tile the box meets
        self.assertEqual([w[0] for w in lk.lighting(11, 12)], [100])
        pk = self.places(self.out_box)                   # places stay whole-file
        self.assertTrue(pk.complete(14, 14))
        self.assertIn('way/104', self.ids(pk.places(13, 12)))

    def test_merge_keeps_the_higher_version(self):
        pk = self.places(self.out_ab)
        p2 = [e for e in pk.places(10, 10) if e['id'] == 2][0]
        self.assertEqual(p2['tags']['name'], 'Newer Pharmacy')
        p3 = [e for e in pk.places(12, 10) if e['id'] == 3][0]
        self.assertNotIn('name', p3['tags'])          # B's version 0 copy lost

    def _golden(self, out, margin=0, lighting_boxes=None):
        ns = argparse.Namespace(pbf=self.a, index=self.index, polygon_id='synth/a',
                                places_pack=os.path.join(out, 'places.pack'),
                                lighting_pack=os.path.join(out, 'lighting.pack'),
                                lighting_boxes=lighting_boxes, margin_tiles=margin, box=None)
        return golden.run(ns)

    def test_golden_agrees_on_the_same_file(self):
        rep = self._golden(self.out_a)
        self.assertTrue(rep['ok'], json.dumps(rep['failures'][:5]))
        self.assertEqual(rep['domain_tiles'], 64)
        self.assertEqual(rep['places']['left_out_missing_members'], 2)   # (15,15) and (12,13)
        rep = self._golden(self.out_box, lighting_boxes=self.regions)
        self.assertTrue(rep['ok'], json.dumps(rep['failures'][:5]))
        self.assertEqual(rep['lighting']['tiles_compared'], 1)

    def test_golden_notices_a_difference(self):
        rep = self._golden(self.out_ab)
        self.assertFalse(rep['ok'])
        self.assertEqual(sorted(f['tile'] for f in rep['failures']), ['10,10'])
        self.assertIn('tags differ on node/2', rep['failures'][0]['why'])

    def test_golden_query_text_parses_to_the_rules(self):
        q = golden.parse_query(golden.PLACES_QUERY)
        self.assertEqual(len(q), 9)
        self.assertEqual(q[-1], ('way', [('shop', '~', '^(convenience|supermarket)$'), ('opening_hours', '~', '24/7')]))
        self.assertEqual(golden.parse_query(golden.LIGHTING_QUERY),
                         [('way', [('highway', 'exists', ''), ('lit', 'exists', '')])])


if __name__ == '__main__':
    unittest.main()
