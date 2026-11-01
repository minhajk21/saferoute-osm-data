"""Shared test helpers: repo on sys.path, synthetic OSM files, a fake Geofabrik index."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import osmium  # noqa: E402
from osmium.osm import mutable  # noqa: E402

E7 = 10_000_000
T = 200_000


def deg(units):
    """1e-7 degree integer -> float degrees that pyosmium stores back exactly."""
    return units / E7


def write_osm(path, nodes=(), ways=(), relations=(), timestamp='2026-10-30T21:24:51Z'):
    """nodes: (id, x, y, tags[, version]) with x/y in 1e-7 degree;
    ways: (id, [node ids], tags[, version]); relations: (id, [(type, ref, role)], tags[, version])."""
    if os.path.exists(path):
        os.remove(path)
    h = osmium.io.Header()
    if timestamp:
        h.set('osmosis_replication_timestamp', timestamp)
    with osmium.SimpleWriter(path, header=h) as w:
        for n in sorted(nodes, key=lambda n: n[0]):
            w.add_node(mutable.Node(id=n[0], location=(deg(n[1]), deg(n[2])), tags=n[3],
                                    version=n[4] if len(n) > 4 else 1))
        for wy in sorted(ways, key=lambda w_: w_[0]):
            w.add_way(mutable.Way(id=wy[0], nodes=wy[1], tags=wy[2], version=wy[3] if len(wy) > 3 else 1))
        for r in sorted(relations, key=lambda r_: r_[0]):
            w.add_relation(mutable.Relation(id=r[0], members=r[1], tags=r[2], version=r[3] if len(r) > 3 else 1))


def square(x0, y0, x1, y1):
    """GeoJSON Polygon coordinates (degrees) of a box given in 1e-7 units."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {'type': 'Polygon', 'coordinates': [[[deg(x), deg(y)] for x, y in pts]]}


def write_index(path, polygons):
    """polygons: {id: geometry}. Writes an index-v1.json look-alike."""
    feats = [{'type': 'Feature', 'properties': {'id': k, 'urls': {}}, 'geometry': g} for k, g in polygons.items()]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'type': 'FeatureCollection', 'features': feats}, f)


def write_regions(path, regions):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'regions': regions}, f)
