#!/usr/bin/env python3
"""Read one Geofabrik extract and add what the packs need to the store.

Usage:
  python3 -I pipeline/extract.py --pbf FILE.osm.pbf --source ID --store STORE.sqlite \
      --lighting whole|boxes --regions config/regions.json [--info INFO.json]

Four or five reads of the file, each of one object type and filtered in C++
(pyosmium 4), so Python only sees candidates and no full node-location index
is ever built (the UK has ~300 M nodes; only the ~10 M the kept ways use are
looked up):
  R   relations, amenity=hospital: the place relations and their members;
  W   ways with a lit/amenity/shop key: lit highways, place ways, member
      ways (their node ids); then, by id, member ways W did not see;
  N1  nodes with an amenity/shop key: place nodes;
  N2  nodes by id (pyosmium IdTracker): every node a kept way or a place
      relation needs, into a sparse location table.
In --lighting boxes mode a lit way is kept only if it touches a tile that
meets a widened region box; place data is always kept for the whole file.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from array import array

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osmium  # noqa: E402

from pipeline import regions, rules, store, tiles  # noqa: E402


def header_timestamp(path):
    """The extract's osmosis_replication_timestamp (ISO 8601 text) or None."""
    with osmium.io.Reader(str(path), osmium.osm.NOTHING) as rd:
        return rd.header().get('osmosis_replication_timestamp') or None


def iso_epoch(text):
    return int(dt.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp())


def _touches_mask(xy, mask):
    for tx, ty in tiles.path_tiles(xy):
        row = mask.get(ty)
        if row is not None and tx in row:
            return True
    return False


def extract(pbf, source_id, db, lighting_mode, mask=None, info=None):
    if lighting_mode not in ('whole', 'boxes'):
        raise ValueError('lighting mode must be whole or boxes')
    if lighting_mode == 'boxes' and mask is None:
        raise ValueError('boxes mode needs the lighting mask')
    pbf = str(pbf)
    stamp = header_timestamp(pbf)
    if not stamp:
        raise RuntimeError(f'{pbf}: no osmosis_replication_timestamp in the header')
    ts = iso_epoch(stamp)
    timings = {}
    stats = {'lit_ways_seen': 0, 'lit_ways_kept': 0, 'lit_points_kept': 0, 'lit_missing_nodes': 0,
             'places': {'node': 0, 'way': 0, 'relation': 0}, 'place_missing_nodes': 0,
             'relation_members_wanted': 0, 'relation_members_not_in_file': 0, 'node_locations_read': 0}
    NODE, WAY, REL = osmium.osm.NODE, osmium.osm.WAY, osmium.osm.RELATION
    flt = osmium.filter

    # ---- R: hospital relations
    t = time.monotonic()
    rels = {}
    for r in osmium.FileProcessor(pbf, REL).with_filter(flt.TagFilter(('amenity', 'hospital'))):
        if rules.is_place('r', r.tags):
            rels[r.id] = (r.version, rules.kept_tags(r.tags), [[m.type, m.ref] for m in r.members])
    want_n = {ref for _, _, ms in rels.values() for k, ref in ms if k == 'n'}
    want_w = {ref for _, _, ms in rels.values() for k, ref in ms if k == 'w'}
    stats['relation_members_wanted'] = len(want_n) + len(want_w)
    timings['relations_s'] = round(time.monotonic() - t, 2)

    # ---- W: lit highways, place ways, member ways (node refs only)
    t = time.monotonic()
    need = osmium.IdTracker()           # nodes whose locations are needed
    lit_w, place_w, member_w = [], [], {}
    for w in osmium.FileProcessor(pbf, WAY).with_filter(flt.KeyFilter('lit', 'amenity', 'shop')):
        tags = w.tags
        is_lit = rules.is_lit_way(tags)
        is_place = rules.is_place('w', tags)
        is_member = w.id in want_w
        if not (is_lit or is_place or is_member):
            continue
        refs = array('q', (n.ref for n in w.nodes))
        need.add_references(w)
        if is_lit:
            lit_w.append((w.id, w.version, tags['lit'], refs))
        if is_place:
            place_w.append((w.id, w.version, json.dumps(rules.kept_tags(tags), sort_keys=True), refs))
        if is_member:
            member_w[w.id] = (w.version, refs)
    rest_w = want_w - member_w.keys()
    if rest_w:
        for w in osmium.FileProcessor(pbf, WAY).with_filter(flt.IdFilter(rest_w)):
            member_w[w.id] = (w.version, array('q', (n.ref for n in w.nodes)))
            need.add_references(w)
    timings['ways_s'] = round(time.monotonic() - t, 2)

    # ---- N: place nodes and member nodes, then the locations the ways need
    t = time.monotonic()
    place = store.Batch(db, 'place')
    mnode = store.Batch(db, 'member_node')
    got_n = set()
    for n in osmium.FileProcessor(pbf, NODE).with_filter(flt.KeyFilter('amenity', 'shop')):
        if rules.is_place('n', n.tags):
            loc = n.location
            place.add((store.KIND['n'], n.id, n.version, ts, source_id,
                       json.dumps(rules.kept_tags(n.tags), sort_keys=True), store.pack_xy((loc.x, loc.y)), 0, None))
            stats['places']['node'] += 1
    for nid in want_n:
        need.add_node(nid)
    locs = osmium.index.create_map('sparse_mem_array')
    for n in osmium.FileProcessor(pbf, NODE).with_filter(need.id_filter()):
        locs.set(n.id, n.location)
        stats['node_locations_read'] += 1
        if n.id in want_n:
            mnode.add((n.id, n.version, ts, n.location.x, n.location.y))
            got_n.add(n.id)
    timings['nodes_s'] = round(time.monotonic() - t, 2)

    # ---- resolve and store
    t = time.monotonic()

    def xy_of(refs):
        xy, missing = [], 0
        for ref in refs:
            try:
                loc = locs.get(ref)
            except KeyError:
                missing += 1
                continue
            if loc.valid():
                xy.append(loc.x)
                xy.append(loc.y)
            else:
                missing += 1
        return xy, missing

    lit = store.Batch(db, 'lit_way')
    for wid, version, value, refs in lit_w:
        stats['lit_ways_seen'] += 1
        xy, missing = xy_of(refs)
        if xy and (lighting_mode == 'whole' or _touches_mask(xy, mask)):
            lit.add((wid, version, ts, source_id, value, store.pack_xy(xy), missing))
            stats['lit_ways_kept'] += 1
            stats['lit_points_kept'] += len(xy) // 2
            stats['lit_missing_nodes'] += missing
    for wid, version, tags_json, refs in place_w:
        xy, missing = xy_of(refs)
        place.add((store.KIND['w'], wid, version, ts, source_id, tags_json, store.pack_xy(xy), missing, None))
        stats['places']['way'] += 1
        stats['place_missing_nodes'] += missing
    mway = store.Batch(db, 'member_way')
    for wid, (version, refs) in member_w.items():
        xy, missing = xy_of(refs)
        mway.add((wid, version, ts, store.pack_xy(xy), missing))
    for rid, (version, tags, members) in rels.items():
        place.add((store.KIND['r'], rid, version, ts, source_id, json.dumps(tags, sort_keys=True), None, 0,
                   json.dumps(members)))
        stats['places']['relation'] += 1
    stats['relation_members_not_in_file'] = len(want_w - member_w.keys()) + len(want_n - got_n)
    for b in (lit, place, mnode, mway):
        b.flush()
    db.commit()
    timings['resolve_store_s'] = round(time.monotonic() - t, 2)

    record = dict(info or {})
    record.update({'source': source_id, 'path_name': os.path.basename(pbf), 'timestamp': stamp,
                   'timestamp_epoch': ts, 'lighting': lighting_mode, 'stats': stats, 'timings': timings})
    store.put_source(db, source_id, record)
    return record


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pbf', required=True)
    ap.add_argument('--source', required=True, help='Geofabrik id, e.g. us/district-of-columbia')
    ap.add_argument('--store', required=True)
    ap.add_argument('--lighting', choices=('whole', 'boxes'), required=True)
    ap.add_argument('--regions', required=True)
    ap.add_argument('--info', help='JSON file with download facts to record (url, bytes, md5, sha256)')
    a = ap.parse_args(argv)
    mask = regions.lighting_mask(regions.load(a.regions)) if a.lighting == 'boxes' else None
    info = json.load(open(a.info, encoding='utf-8')) if a.info else None
    db = store.open_store(a.store)
    rec = extract(a.pbf, a.source, db, a.lighting, mask, info)
    db.commit()
    db.close()
    print(json.dumps(rec, indent=1, sort_keys=True))


if __name__ == '__main__':
    main()
