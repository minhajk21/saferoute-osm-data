#!/usr/bin/env python3
"""Size probe for the DESIGN.md pack formats (not the pipeline).

Usage: python3 -I tools/measure.py EXTRACT.osm.pbf

Reads one Geofabrik extract with pyosmium and reports:
  lighting  ways with highway=* and lit=* (lighting.js's Overpass query),
            each stored with its full geometry in every 0.02 degree tile one
            of its segments touches, delta/zigzag/varint coded at 1e-7 deg,
            deflated per tile;
  places    what safePlaces.js tileQuery() selects (nodes/ways/relations
            amenity=hospital, nodes/ways amenity=police|pharmacy, nodes/ways
            shop=convenience|supermarket with opening_hours containing 24/7),
            as Overpass-shaped JSON with only the tags the backend reads,
            in every tile its geometry touches, deflated per tile.
Relation centres use member nodes and member ways' nodes (sub-relations
ignored) - the assumption DESIGN.md asks the golden test to confirm.
"""
import json
import math
import re
import sys
import zlib
from collections import defaultdict

import osmium

TILE = 0.02
E7 = 10_000_000
PLACE_TAGS = (
    'amenity', 'shop', 'opening_hours', 'emergency', 'beds', 'healthcare',
    'healthcare:speciality', 'name', 'name:en', 'alt_name', 'official_name',
    'wikidata', 'operator', 'brand', 'phone', 'contact:phone',
    'addr:street', 'addr:housenumber', 'addr:city', 'addr:postcode',
)
SHOP_24 = re.compile(r'24/7')  # Overpass ["opening_hours"~"24/7"]


def tile_of(x, y):
    return (math.floor(x / TILE), math.floor(y / TILE))


def seg_tiles(ax, ay, bx, by):
    """Tiles whose closed rectangle a segment touches (Liang-Barsky)."""
    out = []
    for tx in range(math.floor(min(ax, bx) / TILE), math.floor(max(ax, bx) / TILE) + 1):
        for ty in range(math.floor(min(ay, by) / TILE), math.floor(max(ay, by) / TILE) + 1):
            x0, x1, y0, y1 = tx * TILE, (tx + 1) * TILE, ty * TILE, (ty + 1) * TILE
            t0, t1 = 0.0, 1.0
            dx, dy = bx - ax, by - ay
            ok = True
            for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
                if p == 0:
                    if q < 0:
                        ok = False
                        break
                else:
                    r = q / p
                    if p < 0:
                        t0 = max(t0, r)
                    else:
                        t1 = min(t1, r)
                    if t0 > t1:
                        ok = False
                        break
            if ok:
                out.append((tx, ty))
    return out


def varint(n, buf):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def zz(n):
    return (n << 1) ^ (n >> 63)


def place_kind(o, kind):
    t = o.tags
    if t.get('amenity') == 'hospital':
        return True
    if kind == 'r':
        return False
    if t.get('amenity') in ('police', 'pharmacy'):
        return True
    if t.get('shop') in ('convenience', 'supermarket') and SHOP_24.search(t.get('opening_hours', '')):
        return True
    return False


def main():
    path = sys.argv[1]
    hdr = osmium.io.Reader(path, osmium.osm.osm_entity_bits.NOTHING)
    stamp = hdr.header().get('osmosis_replication_timestamp')
    hdr.close()

    # ---- pass 1: lit ways + place nodes/ways + hospital relation members
    lit_ways = []                 # (id, lit, [(x7, y7), ...])
    places = []                   # dicts, Overpass-shaped, plus _tiles
    rel_members = {}              # rel id -> (tags, [('n'|'w', ref)])
    for o in osmium.FileProcessor(path).with_locations():
        if o.is_node():
            if o.tags and place_kind(o, 'n'):
                x, y = o.location.lon, o.location.lat
                places.append({'type': 'node', 'id': o.id, 'lat': round(y, 7), 'lon': round(x, 7),
                               'tags': {k: o.tags[k] for k in PLACE_TAGS if k in o.tags},
                               '_tiles': {tile_of(x, y)}})
        elif o.is_way():
            t = o.tags
            if 'highway' in t and 'lit' in t:
                pts = [(n.location.x, n.location.y) for n in o.nodes if n.location.valid()]
                lit_ways.append((o.id, t['lit'], pts))
            if place_kind(o, 'w'):
                pts = [(n.location.lon, n.location.lat) for n in o.nodes if n.location.valid()]
                if not pts:
                    continue
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                tiles = set()
                for a, b in zip(pts, pts[1:] or pts):
                    tiles.update(seg_tiles(a[0], a[1], b[0], b[1]))
                places.append({'type': 'way', 'id': o.id,
                               'center': {'lat': round((min(ys) + max(ys)) / 2, 7),
                                          'lon': round((min(xs) + max(xs)) / 2, 7)},
                               'tags': {k: t[k] for k in PLACE_TAGS if k in t}, '_tiles': tiles})
        elif o.is_relation():
            if o.tags.get('amenity') == 'hospital':
                rel_members[o.id] = ({k: o.tags[k] for k in PLACE_TAGS if k in o.tags},
                                     [(m.type, m.ref) for m in o.members])

    # ---- pass 2: geometry of hospital relation members
    want_n = {r for _, ms in rel_members.values() for t, r in ms if t == 'n'}
    want_w = {r for _, ms in rel_members.values() for t, r in ms if t == 'w'}
    node_xy, way_xy = {}, {}
    if rel_members:
        for o in osmium.FileProcessor(path).with_locations():
            if o.is_node() and o.id in want_n:
                node_xy[o.id] = (o.location.lon, o.location.lat)
            elif o.is_way() and o.id in want_w:
                way_xy[o.id] = [(n.location.lon, n.location.lat) for n in o.nodes if n.location.valid()]
    rel_incomplete = 0
    for rid, (tags, ms) in rel_members.items():
        pts, tiles, missing = [], set(), 0
        for t, r in ms:
            if t == 'n':
                if r in node_xy:
                    pts.append(node_xy[r])
                    tiles.add(tile_of(*node_xy[r]))
                else:
                    missing += 1
            elif t == 'w':
                if r in way_xy and way_xy[r]:
                    w = way_xy[r]
                    pts.extend(w)
                    for a, b in zip(w, w[1:] or w):
                        tiles.update(seg_tiles(a[0], a[1], b[0], b[1]))
                else:
                    missing += 1
        if missing:
            rel_incomplete += 1
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        places.append({'type': 'relation', 'id': rid,
                       'center': {'lat': round((min(ys) + max(ys)) / 2, 7), 'lon': round((min(xs) + max(xs)) / 2, 7)},
                       'tags': tags, '_tiles': tiles})

    # ---- lighting pack estimate
    tile_ways = defaultdict(list)
    lit_values = {}
    total_pts = 0
    for wid, lit, pts in lit_ways:
        total_pts += len(pts)
        lit_values.setdefault(lit, len(lit_values))
        tiles = set()
        for a, b in zip(pts, pts[1:]):
            tiles.update(seg_tiles(a[0] / E7, a[1] / E7, b[0] / E7, b[1] / E7))
        for t in tiles:
            tile_ways[t].append((wid, lit_values[lit], pts))
    raw = comp = members = mem_pts = 0
    max_comp = max_pts = 0
    for (tx, ty), ways in tile_ways.items():
        buf = bytearray()
        varint(len(ways), buf)
        prev_id = 0
        ox, oy = round(tx * TILE * E7), round(ty * TILE * E7)
        tpts = 0
        for wid, li, pts in sorted(ways):
            varint(wid - prev_id, buf)
            prev_id = wid
            varint(li, buf)
            varint(len(pts), buf)
            px, py = ox, oy
            for x, y in pts:
                varint(zz(x - px), buf)
                varint(zz(y - py), buf)
                px, py = x, y
            tpts += len(pts)
        members += len(ways)
        mem_pts += tpts
        raw += len(buf)
        c = len(zlib.compress(bytes(buf), 9))
        comp += c
        max_comp = max(max_comp, c)
        max_pts = max(max_pts, tpts)

    # ---- places pack estimate
    tile_places = defaultdict(list)
    kinds = defaultdict(int)
    for p in places:
        a = p['tags'].get('amenity') or ('shop' if 'shop' in p['tags'] else '?')
        kinds[f"{p['type']}:{a}"] += 1
        for t in p.pop('_tiles'):
            tile_places[t].append(p)
    praw = pcomp = pmax = 0
    for t, ps in tile_places.items():
        s = json.dumps(sorted(ps, key=lambda e: (e['type'], e['id'])), separators=(',', ':'), ensure_ascii=False).encode()
        praw += len(s)
        c = len(zlib.compress(s, 9))
        pcomp += c
        pmax = max(pmax, c)

    print(json.dumps({
        'file': path.rsplit('/', 1)[-1], 'replication_timestamp': stamp,
        'lighting': {'ways': len(lit_ways), 'points': total_pts, 'distinct_lit_values': len(lit_values),
                     'tiles': len(tile_ways), 'tile_memberships': members,
                     'dup_factor_points': round(mem_pts / max(1, total_pts), 3),
                     'raw_bytes': raw, 'deflated_bytes': comp, 'max_tile_deflated': max_comp,
                     'max_tile_points': max_pts,
                     'max_tile_typed_bytes_est': max_pts * 8},
        'places': {'elements': len(places), 'by_kind': dict(sorted(kinds.items())),
                   'hospital_relations_with_missing_members': rel_incomplete,
                   'tiles': len(tile_places), 'raw_json_bytes': praw, 'deflated_bytes': pcomp,
                   'max_tile_deflated': pmax},
    }, indent=1))


if __name__ == '__main__':
    main()
