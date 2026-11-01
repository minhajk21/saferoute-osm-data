#!/usr/bin/env python3
"""Write the backend's committed strict area (data/osm-strict-area.json there).

Usage: python3 -I tools/strict_area.py OUT_JSON

The tiles where the backend answers lighting and Find Help only from our
copy and never asks Overpass (pipeline/strict.py): every tile meeting a city
box of config/regions.json or the app's UK outline. The backend reads this
file at boot, so it knows the area even when no copy could be loaded (then
it refuses there, rather than send a lookup to Overpass). Rerun and commit
the output in the backend whenever regions.json or uk_outline.json changes;
the backend's test/osm-regions.test.js checks every provider's covers() box
against it.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import coverage, regions, strict  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    out = sys.argv[1]
    reg_path = os.path.join(ROOT, 'config', 'regions.json')
    uk_path = os.path.join(ROOT, 'config', 'uk_outline.json')
    cfg = regions.load(reg_path)
    tiles = strict.strict_tiles(cfg, uk_path)
    rows = coverage.to_runs(strict.by_row(tiles))
    doc = {
        '_comment': ('STRICT AREA for our OSM copy (saferoute-osm-data pipeline/strict.py; written by its '
                     'tools/strict_area.py, do not edit by hand). Inside these 0.02 degree tiles (x = floor(lng / 0.02), '
                     'y = floor(lat / 0.02)) street lighting and Find Help are answered only from our copy, never by '
                     'Overpass: every tile meeting a city box (the covers() boxes) or the app\'s UK outline. Rows are '
                     '[y, [[x0, x1], ...]], runs inclusive. Every release must have every one of these tiles complete '
                     'in places.pack (its sanity check), so a loaded copy can always answer here.'),
        'schema': 1,
        'tile_size_e7': 200_000,
        'tiles': len(tiles),
        'regions': [{'name': r['name'], 'box': r['box']} for r in cfg['regions']],
        'uk_outline': 'Great Britain, Northern Ireland, the Isle of Man and the Channel Islands to their territorial limits',
        'built_from': {'regions.json': sha(reg_path), 'uk_outline.json': sha(uk_path)},
        'rows': [[y, [list(r) for r in runs]] for y, runs in rows],
    }
    with open(out, 'w', encoding='utf-8') as f:
        f.write(json.dumps(doc, separators=(',', ':')) + '\n')
    print(json.dumps({'tiles': len(tiles), 'rows': len(rows), 'runs': sum(len(r) for _, r in rows),
                      'bytes': os.path.getsize(out)}))


if __name__ == '__main__':
    main()
