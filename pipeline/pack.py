#!/usr/bin/env python3
"""Merge the store into lighting.pack and places.pack.

Usage:
  python3 -I pipeline/pack.py --store STORE.sqlite --index index-v1.json \
      --regions config/regions.json --out DIR

Each element goes into EVERY tile its geometry touches (closed tiles, DESIGN.md
A1/A2/A6), so a tile's record is the Overpass answer for that tile. Only
COMPLETE tiles are written (pipeline/coverage.py), minus any tile an element
with missing nodes or members touches, plus the tiles reviewed as empty
(config/reviewed_empty.json, pipeline/strict.py) that no element touches.
Writes DIR/lighting.pack, DIR/places.pack and DIR/pack-report.json.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import coverage, packfmt, regions, store, strict, tiles  # noqa: E402

TYPE_ORDER = {'node': 0, 'way': 1, 'relation': 2}


def load_index(path):
    with open(path, encoding='utf-8') as f:
        idx = json.load(f)
    return {feat['properties']['id']: feat for feat in idx['features']}


def _deg(v):
    return v / tiles.E7


def _center(xs, ys):
    """Overpass `out center`: middle of the bounding box, printed to 7 decimals."""
    lat = (_deg(min(ys)) + _deg(max(ys))) / 2
    lon = (_deg(min(xs)) + _deg(max(xs))) / 2
    return {'lat': float(f'{lat:.7f}'), 'lon': float(f'{lon:.7f}')}


def _remove(cov, bad):
    for tx, ty in bad:
        row = cov.get(ty)
        if row is not None:
            row.discard(tx)


def _is_complete(cov, tx, ty):
    row = cov.get(ty)
    return row is not None and tx in row


def _add_reviewed(cov, reviewed, touched):
    """Marks each reviewed tile complete unless data touches it. Returns
    (added, already complete, contradicted: [(tile, group)])."""
    added = already = 0
    contradicted = []
    for t, group in sorted(reviewed.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        tx, ty = t
        if t in touched:
            contradicted.append((f'{tx},{ty}', group))
        elif _is_complete(cov, tx, ty):
            already += 1
        else:
            cov.setdefault(ty, set()).add(tx)
            added += 1
    return added, already, contradicted


def build(store_path, index_path, regions_path, out_dir, built_at=None, reviewed_path=None):
    t_start = time.monotonic()
    built_at = int(built_at if built_at is not None else time.time())
    os.makedirs(out_dir, exist_ok=True)
    db = store.open_store(store_path)
    srcs = store.sources(db)
    if not srcs:
        raise RuntimeError('the store has no sources')
    feats = load_index(index_path)
    cfg = regions.load(regions_path)
    mask = regions.lighting_mask(cfg)
    data_as_of = min(s['timestamp_epoch'] for s in srcs.values())

    # ---- coverage
    t = time.monotonic()
    place_polys, light_polys = [], []
    for sid, s in srcs.items():
        pid = s.get('polygon', sid)
        if pid not in feats:
            raise RuntimeError(f'{sid}: polygon {pid} not in the index')
        edges = coverage.geometry_edges(feats[pid]['geometry'])
        place_polys.append(coverage.Polygon(edges))
        light_polys.append(coverage.Polygon(edges, mask=None if s['lighting'] == 'whole' else mask))
    place_cov = coverage.complete_tiles(place_polys)
    light_cov = coverage.complete_tiles(light_polys)
    cov_s = time.monotonic() - t

    report = {'built_at': built_at, 'data_as_of': data_as_of, 'sources': sorted(srcs)}

    # ---- places
    t = time.monotonic()
    mnodes = {i: (x, y) for i, x, y in db.execute('SELECT id, x, y FROM member_node')}
    mways = {i: (store.unpack_xy(xy), missing) for i, xy, missing in
             db.execute('SELECT id, xy, missing FROM member_way')}
    tile_elems = defaultdict(list)
    place_bad = set()
    pstats = {'node': 0, 'way': 0, 'relation': 0, 'unplaceable': 0, 'with_missing_data': 0}
    by_kind = defaultdict(int)
    positions = []
    for kind, oid, tags_json, xy_blob, missing, members_json in db.execute(
            'SELECT kind, id, tags, xy, missing, members FROM place ORDER BY kind, id'):
        tname = store.KIND_NAME[kind]
        tags = json.loads(tags_json)
        incomplete = bool(missing)
        if kind == 0:
            x, y = store.unpack_xy(xy_blob)
            tl = set(tiles.point_tiles(x, y))
            el = {'type': 'node', 'id': oid, 'lat': _deg(y), 'lon': _deg(x), 'tags': tags}
            pos = (x, y)
        else:
            if kind == 1:
                pts = store.unpack_xy(xy_blob)
                tl = tiles.path_tiles(pts)
                xs, ys = pts[0::2], pts[1::2]
            else:
                tl, xs, ys = set(), [], []
                for mtype, ref in json.loads(members_json):
                    if mtype == 'n':
                        p = mnodes.get(ref)
                        if p is None:
                            incomplete = True
                            continue
                        tl.update(tiles.point_tiles(*p))
                        xs.append(p[0])
                        ys.append(p[1])
                    elif mtype == 'w':
                        w = mways.get(ref)
                        if w is None:
                            incomplete = True
                            continue
                        wxy, wmiss = w
                        if wmiss:
                            incomplete = True
                        tl.update(tiles.path_tiles(wxy))
                        xs.extend(wxy[0::2])
                        ys.extend(wxy[1::2])
            if not xs:
                pstats['unplaceable'] += 1
                continue
            el = {'type': tname, 'id': oid, 'center': _center(xs, ys), 'tags': tags}
            pos = (tiles.to_e7(el['center']['lon']), tiles.to_e7(el['center']['lat']))
        if incomplete:
            pstats['with_missing_data'] += 1
            place_bad.update(tl)
        pstats[tname] += 1
        by_kind[f"{tname}:{tags.get('amenity') or ('shop' if 'shop' in tags else '?')}"] += 1
        positions.append(pos)
        for t_ in tl:
            tile_elems[t_].append(el)
    _remove(place_cov, place_bad)
    reviewed = strict.load_reviewed(reviewed_path)
    p_added, p_already, p_contra = _add_reviewed(place_cov, reviewed['places'], tile_elems)
    places_path = os.path.join(out_dir, 'places.pack')
    pw = packfmt.PackWriter(places_path, packfmt.KIND_PLACES, data_as_of, built_at)
    ptiles = pmemb = 0
    pmax = 0
    for tx, ty in sorted(tile_elems, key=lambda k: (k[1], k[0])):
        if not _is_complete(place_cov, tx, ty):
            continue
        els = sorted(tile_elems[(tx, ty)], key=lambda e: (TYPE_ORDER[e['type']], e['id']))
        n = pw.add_tile(tx, ty, packfmt.encode_places_tile(els), len(els))
        pmax = max(pmax, n)
        ptiles += 1
        pmemb += len(els)
    psize = pw.close([], coverage.to_runs(place_cov))
    report['places'] = dict(pstats, by_kind=dict(sorted(by_kind.items())), tiles_with_data=ptiles,
                            tile_memberships=pmemb, complete_tiles=coverage.count(place_cov),
                            tiles_dropped_missing_data=len(place_bad), bytes=psize,
                            max_tile_deflated=pmax, seconds=round(time.monotonic() - t, 2))

    # ---- lighting
    t = time.monotonic()
    strings = [r[0] for r in db.execute('SELECT DISTINCT lit FROM lit_way ORDER BY lit')]
    sidx = {s: i for i, s in enumerate(strings)}
    tile_regions = defaultdict(list)
    for r in cfg['regions']:
        for tt in regions.region_tiles(r, widened=True):
            tile_regions[tt].append(r['name'])
    db.execute('DROP TABLE IF EXISTS lit_member')
    db.execute('CREATE TABLE lit_member (ty INTEGER, tx INTEGER, id INTEGER, PRIMARY KEY (ty, tx, id)) WITHOUT ROWID')
    light_bad = set()
    light_touched_reviewed = set()
    rows = []
    for wid, xy_blob, missing in db.execute('SELECT id, xy, missing FROM lit_way'):
        tl = tiles.path_tiles(store.unpack_xy(xy_blob))
        light_touched_reviewed.update(t for t in tl if t in reviewed['lighting'])
        if missing:
            light_bad.update(tl)
        for tx, ty in tl:
            if _is_complete(light_cov, tx, ty):
                rows.append((ty, tx, wid))
        if len(rows) >= 50_000:
            db.executemany('INSERT INTO lit_member VALUES (?,?,?)', rows)
            rows = []
    db.executemany('INSERT INTO lit_member VALUES (?,?,?)', rows)
    _remove(light_cov, light_bad)
    l_added, l_already, l_contra = _add_reviewed(light_cov, reviewed['lighting'], light_touched_reviewed)
    light_path = os.path.join(out_dir, 'lighting.pack')
    lw = packfmt.PackWriter(light_path, packfmt.KIND_LIGHTING, data_as_of, built_at)
    region_ways = defaultdict(set)
    all_ways = set()
    ltiles = lmemb = lpts = 0
    lmax = lmax_pts = 0
    cur, cur_ways = None, []

    def flush():
        nonlocal ltiles, lmemb, lpts, lmax, lmax_pts
        if cur is None or not cur_ways:
            return
        tx, ty = cur
        n = lw.add_tile(tx, ty, packfmt.encode_lighting_tile(tx, ty, cur_ways, tiles.TILE), len(cur_ways))
        pts = sum(len(w[2]) // 2 for w in cur_ways)
        ltiles += 1
        lmemb += len(cur_ways)
        lpts += pts
        lmax = max(lmax, n)
        lmax_pts = max(lmax_pts, pts)

    for ty, tx, wid, lit, xy_blob in db.execute(
            'SELECT m.ty, m.tx, w.id, w.lit, w.xy FROM lit_member m JOIN lit_way w ON w.id = m.id '
            'ORDER BY m.ty, m.tx, m.id'):
        if not _is_complete(light_cov, tx, ty):
            continue
        if (tx, ty) != cur:
            flush()
            cur, cur_ways = (tx, ty), []
        cur_ways.append((wid, sidx[lit], store.unpack_xy(xy_blob)))
        all_ways.add(wid)
        for name in tile_regions.get((tx, ty), ()):
            region_ways[name].add(wid)
    flush()
    lsize = lw.close(strings, coverage.to_runs(light_cov))
    db.execute('DROP TABLE lit_member')
    db.commit()
    total_pts = db.execute('SELECT SUM(LENGTH(xy)) / 8 FROM lit_way').fetchone()[0] or 0
    report['lighting'] = {'ways': len(all_ways), 'ways_in_store': db.execute('SELECT COUNT(*) FROM lit_way').fetchone()[0],
                          'points_in_store': total_pts, 'tiles_with_data': ltiles, 'tile_memberships': lmemb,
                          'points_stored': lpts, 'distinct_lit_values': len(strings),
                          'complete_tiles': coverage.count(light_cov), 'tiles_dropped_missing_data': len(light_bad),
                          'bytes': lsize, 'max_tile_deflated': lmax, 'max_tile_points': lmax_pts,
                          'seconds': round(time.monotonic() - t, 2)}

    # ---- per region
    per_region = {}
    for r in cfg['regions']:
        box = r['box']
        nplaces = sum(1 for x, y in positions if regions.in_box(box, x, y))
        p_hole = sorted(f'{tx},{ty}' for tx, ty in regions.region_tiles(r, widened=False)
                        if not _is_complete(place_cov, tx, ty))
        l_hole = sorted(f'{tx},{ty}' for tx, ty in regions.region_tiles(r, widened=True)
                        if not _is_complete(light_cov, tx, ty))
        per_region[r['name']] = {'places': nplaces, 'lit_ways': len(region_ways.get(r['name'], ())),
                                 'places_incomplete_tiles': p_hole, 'lighting_incomplete_tiles': l_hole}
    report['regions'] = per_region
    report['reviewed_empty'] = {
        'places': {'reviewed': len(reviewed['places']), 'added': p_added, 'already_complete': p_already,
                   'contradicted': [f'{t} ({g})' for t, g in p_contra]},
        'lighting': {'reviewed': len(reviewed['lighting']), 'added': l_added, 'already_complete': l_already,
                     'contradicted': [f'{t} ({g})' for t, g in l_contra]},
    }
    report['coverage_seconds'] = round(cov_s, 2)
    report['seconds'] = round(time.monotonic() - t_start, 2)
    db.close()
    with open(os.path.join(out_dir, 'pack-report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=1, sort_keys=True)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--store', required=True)
    ap.add_argument('--index', required=True, help="Geofabrik index-v1.json (the extracts' polygons)")
    ap.add_argument('--regions', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--reviewed', help='config/reviewed_empty.json (tiles reviewed as empty)')
    a = ap.parse_args(argv)
    rep = build(a.store, a.index, a.regions, a.out, reviewed_path=a.reviewed)
    summary = {k: rep[k] for k in ('data_as_of', 'places', 'lighting', 'seconds')}
    print(json.dumps(summary, indent=1, sort_keys=True))


if __name__ == '__main__':
    main()
