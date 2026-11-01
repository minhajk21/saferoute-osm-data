"""Golden checks on real Geofabrik extracts (skipped unless the files are there).

  OSM_TEST_DATA=DIR      holds district-of-columbia-latest.osm.pbf, index-v1.json
                         and (for OSM_TEST_LONDON=1) greater-london-latest.osm.pbf
  OSM_TEST_LONDON=1      also run Greater London (about 2 minutes)

For each extract: (1) the packer's selection, element by element over the
WHOLE file, equals golden.py's independent re-implementation of the Overpass
tag filters run over the same raw file; (2) the full local build (extract,
pack, golden tile-by-tile in both modes, sanity) passes; (3) known counts.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from tests import helpers
from pipeline import build, golden, regions, store
from pipeline import extract as extract_mod

DATA = os.environ.get('OSM_TEST_DATA')
DC = os.path.join(DATA, 'district-of-columbia-latest.osm.pbf') if DATA else None
LONDON = os.path.join(DATA, 'greater-london-latest.osm.pbf') if DATA else None
INDEX = os.path.join(DATA, 'index-v1.json') if DATA else None
RUN_LONDON = os.environ.get('OSM_TEST_LONDON') == '1'


def selection_matches(test, pbf, sid, mode):
    with tempfile.TemporaryDirectory() as d:
        db = store.open_store(os.path.join(d, 's.sqlite'))
        mask = regions.lighting_mask(regions.load(os.path.join(helpers.ROOT, 'config', 'regions.json')))
        extract_mod.extract(pbf, sid, db, mode, mask if mode == 'boxes' else None)
        packer = {(store.KIND_NAME[k], i) for k, i in db.execute('SELECT kind, id FROM place')}
        packer_lit = {i for (i,) in db.execute('SELECT id FROM lit_way')}
        db.close()
    nodes, ways, rels, _, _, lit = golden.read_extract(pbf)
    emu = {('node', i) for i in nodes} | {('way', i) for i in ways} | {('relation', i) for i in rels}
    test.assertEqual(packer, emu)
    if mode == 'whole':
        test.assertEqual(packer_lit, set(lit))
    else:
        test.assertTrue(packer_lit <= set(lit))
    return len(packer), len(packer_lit)


@unittest.skipUnless(DATA and os.path.exists(DC) and os.path.exists(INDEX), 'set OSM_TEST_DATA to run')
class DCTest(unittest.TestCase):
    def test_selection_equals_overpass_filters(self):
        n_places, n_lit = selection_matches(self, DC, 'us/district-of-columbia', 'whole')
        self.assertEqual((n_places, n_lit), (113, 5896))      # Geofabrik file of 30 Oct 2026 21:24Z

    def test_build_golden_and_sanity(self):
        run_build(self, [('us/district-of-columbia', DC)])


@unittest.skipUnless(DATA and RUN_LONDON and LONDON and os.path.exists(LONDON), 'set OSM_TEST_LONDON=1 to run')
class LondonTest(unittest.TestCase):
    def test_selection_equals_overpass_filters(self):
        n_places, n_lit = selection_matches(self, LONDON, 'greater-london', 'whole')
        self.assertEqual((n_places, n_lit), (1763, 188081))   # Geofabrik file of 31 Oct 2026 21:23Z

    def test_build_golden_and_sanity(self):
        run_build(self, [('us/district-of-columbia', DC), ('greater-london', LONDON)])


def _json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def run_build(test, files):
    """The local build (pipeline/build.py --partial) on just these extracts."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _json(os.path.join(helpers.ROOT, 'config', 'sources.local.json'))
        ids = dict(files)
        cfg['sources'] = [s for s in cfg['sources'] if s['id'] in ids]
        cfg['golden'] = [g for g in cfg['golden'] if g.get('source') in ids]
        src = os.path.join(d, 'sources.json')
        with open(src, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        argv = ['--sources', src, '--regions', os.path.join(helpers.ROOT, 'config', 'regions.json'),
                '--index', INDEX, '--work', os.path.join(d, 'w'), '--out', os.path.join(d, 'o'), '--partial']
        for sid, path in files:
            argv += ['--local', f'{sid}={path}']
        test.assertEqual(build.main(argv), 0)
        rep = _json(os.path.join(d, 'w', 'build-report.json'))
        test.assertTrue(rep['ok'])
        test.assertEqual(sorted(rep['golden']), sorted(g['name'] for g in cfg['golden']))
        for name, res in rep['golden'].items():
            test.assertEqual(sorted(res), ['build', 'same_file'])
            for mode, r in res.items():
                test.assertTrue(r['ok'], (name, mode, r.get('failures')))
                test.assertGreater(r['domain_tiles'], 0)
        test.assertGreaterEqual(rep['sanity']['sentinels_checked'], 5)
        man = _json(os.path.join(d, 'o', 'manifest.json'))
        for name in ('lighting.pack', 'places.pack'):
            with open(os.path.join(d, 'o', name), 'rb') as f:
                test.assertEqual(man['assets'][name]['sha256'], hashlib.sha256(f.read()).hexdigest())
        test.assertEqual(sorted(s['id'] for s in man['sources']), sorted(ids))
        con = sqlite3.connect(os.path.join(d, 'w', 'store.sqlite'))
        test.assertEqual(con.execute('SELECT COUNT(*) FROM source').fetchone()[0], len(files))
        con.close()


if __name__ == '__main__':
    unittest.main()
