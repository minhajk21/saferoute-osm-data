#!/usr/bin/env python3
"""Golden check: a brute-force emulator of the Overpass path, tile by tile.

Usage:
  python3 -I pipeline/golden.py --pbf EXTRACT.osm.pbf --index index-v1.json \
      --polygon-id us/district-of-columbia --places-pack places.pack \
      --lighting-pack lighting.pack [--lighting-boxes config/regions.json] \
      [--margin-tiles 1] [--box minLat,maxLat,minLng,maxLng] [--report OUT.json]

For every 0.02 degree tile of the comparison domain it computes, from the RAW
extract, what the backend's two Overpass queries would return for that tile:

  Find Help  the union in safePlaces.js tileQuery(), `out center tags`
  lighting   lighting.js `way[highway][lit](bbox); out geom;`

and requires the pack's tile to be identical: the same elements in the same
order with the same tags (places: centres within 1e-7 degree; lighting: the
same lit value and every node, exactly). Every domain tile must also be
marked complete in the pack.

Independence: this file imports nothing from pipeline/. It parses the query
text copied from the backend, evaluates tags with its own matcher, reads
every object of the extract through Python (no C++ tag filters), computes
bbox membership with exact rationals (Liang-Barsky), and decodes the packs
with its own reader. What it shares with the packer are only the stated
Overpass semantics (DESIGN.md A1-A6), which the live anchor check (DESIGN.md
6.3) has still to confirm.

The domain is the set of tiles whose 9x9 sample grid lies inside the
extract's polygon, shrunk by --margin-tiles (use 1 when the packs were built
from other files than this one), minus tiles an element with missing nodes or
relation members touches (this file alone cannot answer them).
"""
import argparse
import json
import math
import re
import struct
import sys
import time
import zlib
from fractions import Fraction

import osmium

T = 200_000            # one tile, 1e-7 degree
E7 = 10_000_000

# Statement lines of backend src/services/safePlaces.js tileQuery() at commit
# 34ad0d1 (lines 426-437), verbatim.
PLACES_QUERY = r'''
            node["amenity"="hospital"](${bbox});
            way["amenity"="hospital"](${bbox});
            relation["amenity"="hospital"](${bbox});

            node["amenity"="police"](${bbox});
            way["amenity"="police"](${bbox});

            node["amenity"="pharmacy"](${bbox});
            way["amenity"="pharmacy"](${bbox});

            node["shop"~"^(convenience|supermarket)$"]["opening_hours"~"24/7"](${bbox});
            way["shop"~"^(convenience|supermarket)$"]["opening_hours"~"24/7"](${bbox});
'''
# backend src/services/lighting.js:91 at 34ad0d1.
LIGHTING_QUERY = 'way[highway][lit](${bbox});'

# Every tag key the backend reads from a Find Help element (safePlaces.js at
# 34ad0d1: normaliseElements 445-475, classifyType 480-488, is24_7,
# hospitalNames 623-626, hospitalVerdict 676-735, composeAddress 737-749).
READ_TAGS = frozenset([
    'name', 'name:en', 'alt_name', 'official_name', 'phone', 'contact:phone', 'opening_hours',
    'amenity', 'shop', 'emergency', 'beds', 'healthcare', 'healthcare:speciality', 'wikidata',
    'operator', 'brand', 'addr:street', 'addr:housenumber', 'addr:city', 'addr:postcode',
])

STATEMENT = re.compile(r'^(node|way|relation)((?:\[[^\]]+\])+)\(\$\{bbox\}\);$')
CONDITION = re.compile(r'\[(?:"([^"]+)"|([A-Za-z_:]+))(?:(=|~)"([^"]*)")?\]')


def parse_query(text):
    """[(type, [(key, op, value)])] from Overpass statements, one per line."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = STATEMENT.match(line)
        if not m:
            raise ValueError(f'cannot parse statement {line!r}')
        conds = []
        for qk, bk, op, val in CONDITION.findall(m.group(2)):
            conds.append((qk or bk, op or 'exists', val))
        if ''.join(f'[{c}]' for c in re.findall(r'\[([^\]]+)\]', m.group(2))) != m.group(2):
            raise ValueError(f'unparsed conditions in {line!r}')
        out.append((m.group(1), conds))
    return out


def satisfies(conds, tags):
    """Overpass tag conditions: = is exact, ~ an unanchored case-sensitive regex search."""
    for key, op, val in conds:
        v = tags.get(key)
        if v is None:
            return False
        if op == '=' and v != val:
            return False
        if op == '~' and re.search(val, v) is None:
            return False
    return True


def selected(stmts, osm_type, tags):
    return any(t == osm_type and satisfies(c, tags) for t, c in stmts)


# ---------------------------------------------------------------- geometry

def tiles_on_axis(v):
    """Tiles whose closed interval holds coordinate v."""
    q = v // T
    return (q - 1, q) if v % T == 0 else (q,)


def seg_hits_rect(ax, ay, bx, by, x0, y0, x1, y1):
    """Liang-Barsky in exact rationals: does AB meet the closed rectangle?"""
    if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
        return False
    t0, t1 = Fraction(0), Fraction(1)
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        r = Fraction(q, p)
        if p < 0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return True


def point_tile_set(x, y):
    return {(tx, ty) for tx in tiles_on_axis(x) for ty in tiles_on_axis(y)}


def line_tile_set(pts):
    """Tiles a polyline [(x, y), ...] touches: every tile of its bounding box tested segment by segment."""
    if len(pts) == 1:
        return point_tile_set(*pts[0])
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    txs = range(min(tiles_on_axis(min(xs))), max(tiles_on_axis(max(xs))) + 1)
    tys = range(min(tiles_on_axis(min(ys))), max(tiles_on_axis(max(ys))) + 1)
    if len(txs) == 1 and len(tys) == 1:
        return {(txs[0], tys[0])}
    out = set()
    for ty in tys:
        for tx in txs:
            x0, y0 = tx * T, ty * T
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if seg_hits_rect(ax, ay, bx, by, x0, y0, x0 + T, y0 + T):
                    out.add((tx, ty))
                    break
    return out


def ring_contains(ring, x, y):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def domain_tiles(geometry, margin, box=None):
    """Tiles whose 9x9 sample grid is inside the polygon, shrunk by `margin` tiles."""
    polys = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    rings = [[(p[0], p[1]) for p in r] for poly in polys for r in poly]
    allx = [p[0] for r in rings for p in r]
    ally = [p[1] for r in rings for p in r]

    def inside(x, y):
        n = sum(1 for r in rings if ring_contains(r, x, y))
        return n % 2 == 1

    tx0, tx1 = math.floor(min(allx) / 0.02), math.floor(max(allx) / 0.02)
    ty0, ty1 = math.floor(min(ally) / 0.02), math.floor(max(ally) / 0.02)
    full = set()
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            ok = True
            for i in range(9):
                for j in range(9):
                    if not inside((tx + i / 8) * 0.02, (ty + j / 8) * 0.02):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                full.add((tx, ty))
    dom = {(tx, ty) for tx, ty in full
           if all((tx + i, ty + j) in full for i in range(-margin, margin + 1) for j in range(-margin, margin + 1))}
    if box:
        min_lat, max_lat, min_lng, max_lng = box
        dom = {(tx, ty) for tx, ty in dom
               if tx * 0.02 >= min_lng and (tx + 1) * 0.02 <= max_lng
               and ty * 0.02 >= min_lat and (ty + 1) * 0.02 <= max_lat}
    return dom


def boxes_tiles(regions_path):
    """Tiles meeting any region box widened by its margin_km (the lighting clip)."""
    with open(regions_path, encoding='utf-8') as f:
        cfg = json.load(f)
    out = set()
    for r in cfg['regions']:
        a, b, c, d = r['box']
        km = r.get('margin_km', 0)
        dlat = km / 111.32
        dlng = km / (111.32 * math.cos(math.radians((a + b) / 2)))
        lo_x, hi_x = round((c - dlng) * E7), round((d + dlng) * E7)
        lo_y, hi_y = round((a - dlat) * E7), round((b + dlat) * E7)
        for ty in range(min(tiles_on_axis(lo_y)), max(tiles_on_axis(hi_y)) + 1):
            for tx in range(min(tiles_on_axis(lo_x)), max(tiles_on_axis(hi_x)) + 1):
                out.add((tx, ty))
    return out


# ---------------------------------------------------------------- the emulator

def read_extract(path):
    places_q = parse_query(PLACES_QUERY)
    light_q = parse_query(LIGHTING_QUERY)
    rels = {}
    for r in osmium.FileProcessor(path, osmium.osm.RELATION):
        if selected(places_q, 'relation', r.tags):
            rels[r.id] = (dict(r.tags), [(m.type, m.ref) for m in r.members])
    want_n = {ref for _, ms in rels.values() for t, ref in ms if t == 'n'}
    want_w = {ref for _, ms in rels.values() for t, ref in ms if t == 'w'}
    nodes, ways, lit, mem_n, mem_w = {}, {}, {}, {}, {}
    for o in osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations():
        if o.is_node():
            if o.id in want_n:
                mem_n[o.id] = (o.location.x, o.location.y)
            if len(o.tags) and selected(places_q, 'node', o.tags):
                nodes[o.id] = (o.location.x, o.location.y, dict(o.tags))
            continue
        is_place = len(o.tags) > 0 and selected(places_q, 'way', o.tags)
        is_lit = len(o.tags) > 0 and selected(light_q, 'way', o.tags)
        if not (is_place or is_lit or o.id in want_w):
            continue
        pts, missing = [], 0
        for n in o.nodes:
            if n.location.valid():
                pts.append((n.location.x, n.location.y))
            else:
                missing += 1
        if o.id in want_w:
            mem_w[o.id] = (pts, missing)
        if is_place:
            ways[o.id] = (pts, missing, dict(o.tags))
        if is_lit:
            lit[o.id] = (pts, missing, o.tags['lit'])
    return nodes, ways, rels, mem_n, mem_w, lit


def _center(xs, ys):
    return {'lat': float((Fraction(min(ys)) + max(ys)) / 2 / E7),
            'lon': float((Fraction(min(xs)) + max(xs)) / 2 / E7)}


def emulate(path):
    """{tile: [element]} for Find Help, {tile: [(id, lit, xy)]} for lighting, and tiles to leave out."""
    nodes, ways, rels, mem_n, mem_w, lit = read_extract(path)
    places, lighting = {}, {}
    unsure = set()

    def put(store, tileset, item):
        for t in tileset:
            store.setdefault(t, []).append(item)

    def keep(tags):
        return {k: v for k, v in tags.items() if k in READ_TAGS}

    for nid, (x, y, tags) in nodes.items():
        put(places, point_tile_set(x, y), (0, nid, {'type': 'node', 'id': nid, 'lat': y / E7, 'lon': x / E7,
                                                     'tags': keep(tags)}))
    for wid, (pts, missing, tags) in ways.items():
        if not pts:
            continue
        ts = line_tile_set(pts)
        if missing:
            unsure |= ts
        put(places, ts, (1, wid, {'type': 'way', 'id': wid,
                                  'center': _center([p[0] for p in pts], [p[1] for p in pts]),
                                  'tags': keep(tags)}))
    for rid, (tags, members) in rels.items():
        ts, xs, ys, missing = set(), [], [], False
        for t, ref in members:
            if t == 'n':
                if ref in mem_n:
                    x, y = mem_n[ref]
                    ts |= point_tile_set(x, y)
                    xs.append(x)
                    ys.append(y)
                else:
                    missing = True
            elif t == 'w':
                if ref in mem_w and mem_w[ref][0]:
                    pts, miss = mem_w[ref]
                    missing = missing or bool(miss)
                    ts |= line_tile_set(pts)
                    xs += [p[0] for p in pts]
                    ys += [p[1] for p in pts]
                else:
                    missing = True
        if not xs:
            continue
        if missing:
            unsure |= ts
        put(places, ts, (2, rid, {'type': 'relation', 'id': rid, 'center': _center(xs, ys), 'tags': keep(tags)}))
    light_unsure = set()
    for wid, (pts, missing, value) in lit.items():
        if not pts:
            continue
        ts = line_tile_set(pts)
        if missing:
            light_unsure |= ts
        flat = [c for p in pts for c in p]
        put(lighting, ts, (wid, value, flat))
    for t in places:
        places[t] = [e for _, _, e in sorted(places[t], key=lambda k: (k[0], k[1]))]
    for t in lighting:
        lighting[t].sort(key=lambda w: w[0])
    return places, lighting, unsure, light_unsure, {'nodes': len(nodes), 'ways': len(ways),
                                                     'relations': len(rels), 'lit_ways': len(lit)}


# ---------------------------------------------------------------- pack reader (independent)

class PackReader:
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.b = f.read()
        b = self.b
        if b[:8] != b'SROSMPK1':
            raise ValueError(f'{path}: bad magic')
        self.kind, self.version = b[8], b[9]
        self.tile = struct.unpack_from('<I', b, 12)[0]
        idx_off, idx_n, cov_off, _cov_len, str_off, _str_len = struct.unpack_from('<6I', b, 32)
        if self.tile != T:
            raise ValueError('tile size differs')
        n = struct.unpack_from('<I', b, str_off)[0]
        p = str_off + 4
        self.strings = []
        for _ in range(n):
            ln = struct.unpack_from('<H', b, p)[0]
            self.strings.append(b[p + 2:p + 2 + ln].decode('utf-8'))
            p += 2 + ln
        self.index = {}
        for i in range(idx_n):
            x, y, off, ln, cnt = struct.unpack_from('<iiIII', b, idx_off + 20 * i)
            self.index[(x, y)] = (off, ln, cnt)
        rows = struct.unpack_from('<I', b, cov_off)[0]
        p = cov_off + 4
        self.complete = set()
        for _ in range(rows):
            y, k = struct.unpack_from('<iI', b, p)
            p += 8
            for _ in range(k):
                x0, x1 = struct.unpack_from('<ii', b, p)
                p += 8
                self.complete.update((x, y) for x in range(x0, x1 + 1))

    def payload(self, t):
        rec = self.index.get(t)
        if rec is None:
            return None
        off, ln, cnt = rec
        return zlib.decompress(self.b[off:off + ln], -15), cnt


def _varint(b, p):
    v = s = 0
    while True:
        c = b[p]
        p += 1
        v |= (c & 0x7F) << s
        s += 7
        if c < 128:
            return v, p


def decode_lit(t, raw, strings):
    p = 0
    n, p = _varint(raw, p)
    out, wid = [], 0
    for _ in range(n):
        d, p = _varint(raw, p)
        wid += d
        si, p = _varint(raw, p)
        k, p = _varint(raw, p)
        x, y = t[0] * T, t[1] * T
        flat = []
        for _ in range(k):
            zx, p = _varint(raw, p)
            zy, p = _varint(raw, p)
            x += (zx >> 1) ^ -(zx & 1)
            y += (zy >> 1) ^ -(zy & 1)
            flat += [x, y]
        out.append((wid, strings[si], flat))
    return out


# ---------------------------------------------------------------- compare

def _close(a, b):
    return abs(a - b) <= 1.000001e-7


def compare_places(emu, got):
    if len(emu) != len(got):
        return f'{len(got)} elements in the pack, {len(emu)} expected'
    for e, g in zip(emu, got):
        if (e['type'], e['id']) != (g.get('type'), g.get('id')):
            return f"order/elements differ at {g.get('type')}/{g.get('id')} vs {e['type']}/{e['id']}"
        if e['tags'] != g.get('tags'):
            return f"tags differ on {e['type']}/{e['id']}"
        if e['type'] == 'node':
            if set(g) != {'type', 'id', 'lat', 'lon', 'tags'} or not (_close(e['lat'], g['lat']) and _close(e['lon'], g['lon'])):
                return f"position differs on node/{e['id']}"
        else:
            c = g.get('center') or {}
            if set(g) != {'type', 'id', 'center', 'tags'} or not (
                    _close(e['center']['lat'], c.get('lat', 1e9)) and _close(e['center']['lon'], c.get('lon', 1e9))):
                return f"center differs on {e['type']}/{e['id']}"
    return None


def run(args):
    t0 = time.monotonic()
    with open(args.index, encoding='utf-8') as f:
        feats = {ft['properties']['id']: ft for ft in json.load(f)['features']}
    box = [float(v) for v in args.box.split(',')] if args.box else None
    domain = domain_tiles(feats[args.polygon_id]['geometry'], args.margin_tiles, box)
    places, lighting, unsure, light_unsure, counts = emulate(args.pbf)
    t_emu = time.monotonic() - t0
    rep = {'pbf': args.pbf.rsplit('/', 1)[-1], 'domain_tiles': len(domain), 'emulator_counts': counts,
           'places': {}, 'lighting': {}}
    fails = []

    if args.places_pack:
        pk = PackReader(args.places_pack)
        dom = domain - unsure
        n_el = n_tiles = 0
        for t in sorted(dom, key=lambda k: (k[1], k[0])):
            if t not in pk.complete:
                fails.append({'pack': 'places', 'tile': t, 'why': 'tile not complete in the pack'})
                continue
            emu = places.get(t, [])
            pl = pk.payload(t)
            got = [] if pl is None else json.loads(pl[0].decode('utf-8'))
            if pl is not None and pl[1] != len(got):
                fails.append({'pack': 'places', 'tile': t, 'why': 'index count differs from payload'})
            why = compare_places(emu, got)
            if why:
                fails.append({'pack': 'places', 'tile': t, 'why': why})
            n_el += len(emu)
            n_tiles += 1 if emu else 0
        rep['places'] = {'tiles_compared': len(dom), 'tiles_with_elements': n_tiles, 'element_memberships': n_el,
                         'left_out_missing_members': len(domain & unsure)}
    if args.lighting_pack:
        lk = PackReader(args.lighting_pack)
        dom = domain - light_unsure
        if args.lighting_boxes:
            dom &= boxes_tiles(args.lighting_boxes)
        n_w = n_pts = n_tiles = 0
        for t in sorted(dom, key=lambda k: (k[1], k[0])):
            if t not in lk.complete:
                fails.append({'pack': 'lighting', 'tile': t, 'why': 'tile not complete in the pack'})
                continue
            emu = lighting.get(t, [])
            pl = lk.payload(t)
            got = [] if pl is None else decode_lit(t, pl[0], lk.strings)
            if got != emu:
                ids_e = [w[0] for w in emu]
                ids_g = [w[0] for w in got]
                why = ('ways differ' if ids_e != ids_g else 'lit value or geometry differs')
                fails.append({'pack': 'lighting', 'tile': t, 'why': f'{why}: {len(got)} in pack, {len(emu)} expected'})
            n_w += len(emu)
            n_pts += sum(len(w[2]) // 2 for w in emu)
            n_tiles += 1 if emu else 0
        rep['lighting'] = {'tiles_compared': len(dom), 'tiles_with_ways': n_tiles, 'way_memberships': n_w,
                           'points': n_pts, 'left_out_missing_nodes': len(domain & light_unsure)}
    rep['failures'] = [dict(f, tile=f'{f["tile"][0]},{f["tile"][1]}') for f in fails[:200]]
    rep['failure_count'] = len(fails)
    rep['seconds'] = {'emulator': round(t_emu, 2), 'total': round(time.monotonic() - t0, 2)}
    rep['ok'] = not fails and rep['domain_tiles'] > 0
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pbf', required=True)
    ap.add_argument('--index', required=True)
    ap.add_argument('--polygon-id', required=True)
    ap.add_argument('--places-pack')
    ap.add_argument('--lighting-pack')
    ap.add_argument('--lighting-boxes', help='regions.json: compare lighting only inside the widened boxes')
    ap.add_argument('--margin-tiles', type=int, default=0)
    ap.add_argument('--box', help='minLat,maxLat,minLng,maxLng to restrict the domain')
    ap.add_argument('--report')
    a = ap.parse_args(argv)
    rep = run(a)
    text = json.dumps(rep, indent=1, sort_keys=True)
    if a.report:
        with open(a.report, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
    print(text)
    sys.exit(0 if rep['ok'] else 1)


if __name__ == '__main__':
    main()
