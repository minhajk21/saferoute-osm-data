"""The strict area, and tiles reviewed as empty.

STRICT AREA. The backend's covered regions: every tile meeting a city box of
config/regions.json (unwidened: the backend's covers() boxes) and every tile
meeting the app's UK outline (config/uk_outline.json: Great Britain, Northern
Ireland, the Isle of Man and the Channel Islands to their territorial
limits). Inside it the backend answers street lighting and Find Help ONLY
from our copy and never asks Overpass, so no route box or map square from
there leaves the server (backend src/services/osmData.js, STRICT). That is
only safe if every one of these tiles is complete in places.pack, which the
sanity check requires of every release (sanity.py check_strict). The
backend keeps its own committed copy of this tile set
(data/osm-strict-area.json there, written by tools/strict_area.py).

REVIEWED EMPTY (config/reviewed_empty.json). Some strict tiles lie partly
outside every extract polygon we download: open water between Geofabrik's
Illinois, Indiana and Michigan polygons in Lake Michigan, and sea (plus the
uninhabited Les Minquiers reef) inside the UK outline but outside Geofabrik's
united-kingdom polygon. No polygon makes them complete, yet nothing either
feature returns can exist there. A person reviewed each group (the file
says why); pack.py marks those tiles complete with no data, but only while no
element from any extract touches them: a single place or lit way in one
contradicts the review and fails the build instead.
"""
import json
import os
from collections import defaultdict

from pipeline import coverage, regions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def outline_tiles(path):
    """{(tx, ty)} of every tile the outline's rings meet."""
    with open(path, encoding='utf-8') as f:
        rings = json.load(f)['rings']
    out = set()
    for ring in rings:
        poly = coverage.Polygon(coverage.geometry_edges({'type': 'Polygon', 'coordinates': [ring]}))
        for ry, cols in coverage.touched_tiles(poly).items():
            out.update((tx, ry) for tx in cols)
    return out


def strict_tiles(regions_cfg, outline_path):
    """{(tx, ty)}: the strict area (see the top of this file)."""
    out = set(outline_tiles(outline_path)) if outline_path else set()
    for r in regions_cfg['regions']:
        out.update(regions.region_tiles(r, widened=False))
    return out


def by_row(tile_set):
    rows = defaultdict(set)
    for tx, ty in tile_set:
        rows[ty].add(tx)
    return dict(rows)


def _expand(group, key):
    spec = group.get(key) or {}
    out = set()
    for t in spec.get('tiles', []):
        x, y = (int(v) for v in t.split(','))
        out.add((x, y))
    for r in spec.get('rects', []):
        (x0, x1), (y0, y1) = r['x'], r['y']
        if x1 < x0 or y1 < y0 or (x1 - x0 + 1) * (y1 - y0 + 1) > 10_000:
            raise ValueError(f"reviewed_empty {group.get('name')}: bad rect {r}")
        out.update((x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
    return out


def load_reviewed(path):
    """{'places': {(tx, ty): group name}, 'lighting': {...}} from reviewed_empty.json, or empty."""
    res = {'places': {}, 'lighting': {}}
    if not path or not os.path.exists(path):
        return res
    with open(path, encoding='utf-8') as f:
        doc = json.load(f)
    for g in doc.get('groups', []):
        if not g.get('name') or not g.get('why') or not g.get('reviewed'):
            raise ValueError('reviewed_empty: every group needs a name, a why and a reviewed (who and when)')
        for key in ('places', 'lighting'):
            for t in _expand(g, key):
                res[key].setdefault(t, g['name'])
    return res
