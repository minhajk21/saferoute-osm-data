#!/usr/bin/env python3
"""Sanity checks before a publish (DESIGN.md section 4 step 6).

Usage:
  python3 -I pipeline/sanity.py --report OUT/pack-report.json --places-pack OUT/places.pack \
      --lighting-pack OUT/lighting.pack --sources-report OUT/sources.json \
      [--previous PREVIOUS/manifest.json] [--allow-count-change] [--partial] [--out sanity.json]

  counts     per region and per source, places and lit ways may move -10%/+25%
             against the previous release (small counts: +-10). A failure
             stops the publish unless --allow-count-change (a manual run
             after someone has looked).
  sentinels  config/sentinels.json: each required place present, with its
             amenity, in the complete places tile at its position; the others
             (matched by name near a point) only warn.
  coverage   every incomplete tile in a region box (places) or widened box
             (lighting), and in the app's UK outline, must be listed in
             config/known_incomplete.json (outside every extract polygon).
  strict     the backend never asks Overpass inside the strict area (region
             boxes + UK outline, pipeline/strict.py), so EVERY places tile
             there must be complete (reviewed-empty tiles count); and no
             lighting hole there may lie within 2 tiles of a complete tile
             with data (a hole next to streets is not open water: review it).
             A reviewed-empty tile that data touches fails too.
--partial (local test builds of a few extracts) skips regions and sentinels
the packs have no data for and the UK outline, and reports coverage holes as
warnings (a region's neighbouring extracts are not in such a build).
Exit status 1 when any check fails.
"""
import argparse
import json
import math
import os
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import coverage, packfmt, regions, strict, tiles  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOW, HIGH = 0.90, 1.25
SMALL = 50


def _load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def count_ok(prev, new):
    if prev is None or prev == 0:
        return True
    if prev < SMALL:
        return abs(new - prev) <= 10
    return LOW <= new / prev <= HIGH


def check_counts(report, sources_report, previous):
    errors = []
    if not previous:
        return errors, ['no previous release: count checks skipped (first build)']
    prev_regions = {r['name']: r for r in previous.get('regions', [])}
    for name, r in report['regions'].items():
        p = prev_regions.get(name)
        if not p:
            continue
        for key in ('places', 'lit_ways'):
            if not count_ok(p.get(key), r[key]):
                errors.append(f'region {name}: {key} {p.get(key)} -> {r[key]}')
    prev_sources = {s['id']: s for s in previous.get('sources', [])}
    for sid, s in sources_report.items():
        p = prev_sources.get(sid)
        if not p:
            continue
        for key in ('places', 'lit_ways'):
            if not count_ok(p.get('counts', {}).get(key), s['counts'][key]):
                errors.append(f"source {sid}: {key} {p.get('counts', {}).get(key)} -> {s['counts'][key]}")
    return errors, []


def _tile_of(lat, lng):
    return math.floor(tiles.to_e7(lng) / tiles.TILE), math.floor(tiles.to_e7(lat) / tiles.TILE)


def _haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _pos(el):
    if 'lat' in el:
        return el['lat'], el['lon']
    return el['center']['lat'], el['center']['lon']


def check_sentinels(places, cfg, partial):
    errors, warnings, checked = [], [], 0
    radius = cfg.get('radius_m', 1500)
    for s in cfg['sentinels']:
        label = s.get('name') or s.get('name_re')
        tx, ty = _tile_of(s['lat'], s['lng'])
        if not places.complete(tx, ty):
            if partial:
                continue
            (errors if s['required'] else warnings).append(f'sentinel {label} ({s["region"]}): its tile is not complete')
            continue
        checked += 1
        if s.get('id') is not None:
            found = [e for e in places.places(tx, ty) if e['type'] == s['type'] and e['id'] == s['id']]
            ok = bool(found) and found[0]['tags'].get('amenity') == s['amenity']
            if not ok:
                (errors if s['required'] else warnings).append(
                    f"sentinel {s['type']}/{s['id']} {label} ({s['region']}): missing or no longer amenity={s['amenity']}")
            continue
        rx = re.compile(s['name_re'], re.I)
        dlat = radius / 111_000
        dlng = radius / (111_000 * math.cos(math.radians(s['lat'])))
        hit = False
        for gx in range(_tile_of(s['lat'], s['lng'] - dlng)[0], _tile_of(s['lat'], s['lng'] + dlng)[0] + 1):
            for gy in range(_tile_of(s['lat'] - dlat, s['lng'])[1], _tile_of(s['lat'] + dlat, s['lng'])[1] + 1):
                for e in places.places(gx, gy):
                    t = e['tags']
                    if t.get('amenity') != s['amenity']:
                        continue
                    names = ' | '.join(t.get(k, '') for k in ('name', 'name:en', 'alt_name', 'official_name'))
                    if rx.search(names) and _haversine(s['lat'], s['lng'], *_pos(e)) <= radius:
                        hit = True
        if not hit:
            (errors if s['required'] else warnings).append(
                f"sentinel {label} ({s['region']}): no amenity={s['amenity']} matching /{s['name_re']}/ within {radius} m")
    return errors, warnings, checked


outline_tiles = strict.outline_tiles
NEAR_DATA_TILES = 2


def _has_data(pack, tx, ty):
    return (tx, ty) in pack.index and pack.complete(tx, ty)


def check_strict(report, places, lighting, outline_path, partial, regions_cfg):
    """Errors for the strict area (see the module doc). In a --partial build
    only the regions it has any data for are looked at, as warnings."""
    errors = []
    names = [r['name'] for r in regions_cfg['regions']]
    if partial:
        keep = []
        for r in regions_cfg['regions']:
            t = regions.region_tiles(r, widened=False)
            if any(places.complete(tx, ty) for tx, ty in t):
                keep.append(r)
        area = set()
        for r in keep:
            area.update(regions.region_tiles(r, widened=False))
        names = [r['name'] for r in keep]
    else:
        area = strict.strict_tiles(regions_cfg, outline_path)
    p_holes = sorted((tx, ty) for tx, ty in area if not places.complete(tx, ty))
    if p_holes:
        errors.append(f'strict area: {len(p_holes)} Find Help tiles incomplete, where the backend would refuse '
                      f'rather than ask Overpass, e.g. {[f"{x},{y}" for x, y in p_holes[:5]]}')
    near = []
    for tx, ty in sorted(area):
        if lighting.complete(tx, ty):
            continue
        if any(_has_data(lighting, tx + dx, ty + dy)
               for dx in range(-NEAR_DATA_TILES, NEAR_DATA_TILES + 1)
               for dy in range(-NEAR_DATA_TILES, NEAR_DATA_TILES + 1)):
            near.append(f'{tx},{ty}')
    if near:
        errors.append(f'strict area: {len(near)} lighting tiles incomplete within {NEAR_DATA_TILES} tiles of lit '
                      f'streets (not open water: fix the sources or review them), e.g. {near[:5]}')
    rev = report.get('reviewed_empty') or {}
    for key in ('places', 'lighting'):
        bad = (rev.get(key) or {}).get('contradicted') or []
        if bad:
            errors.append(f'reviewed_empty: {len(bad)} {key} tiles reviewed as empty have data, e.g. {bad[:5]}')
    return errors, {'regions': len(names), 'tiles': len(area), 'places_holes': len(p_holes),
                    'lighting_holes_near_data': len(near)}


def check_coverage(report, known, places, lighting, outline_path, partial, regions_cfg):
    errors, notes = [], []
    boxes = {r['name']: r for r in regions_cfg['regions']}
    for name, r in report['regions'].items():
        k = known['regions'].get(name, {'places': [], 'lighting': []})
        for key, field in (('places', 'places_incomplete_tiles'), ('lighting', 'lighting_incomplete_tiles')):
            holes = set(r[field])
            if partial and name in boxes:
                total = len(regions.region_tiles(boxes[name], widened=(key == 'lighting')))
                if len(holes) == total:
                    continue        # a region this partial build has no data for
            extra = sorted(holes - set(k[key]))
            if extra:
                errors.append(f'region {name}: {len(extra)} {key} tiles incomplete that are not known water, e.g. {extra[:5]}')
            healed = set(k[key]) - holes
            if healed:
                notes.append(f'region {name}: {len(healed)} known-incomplete {key} tiles are now complete')
    if not partial:
        uk = outline_tiles(outline_path)
        for key, pack in (('places', places), ('lighting', lighting)):
            holes = {f'{tx},{ty}' for tx, ty in uk if not pack.complete(tx, ty)}
            extra = sorted(holes - set(known['uk_outline'][key]))
            if extra:
                errors.append(f'UK outline: {len(extra)} {key} tiles incomplete that are not known water, e.g. {extra[:5]}')
    return errors, notes


def run(a):
    report = _load(a.report)
    sources_report = _load(a.sources_report) if a.sources_report else {}
    previous = _load(a.previous) if a.previous and os.path.exists(a.previous) else None
    places = packfmt.Pack(a.places_pack)
    lighting = packfmt.Pack(a.lighting_pack)
    out = {'errors': [], 'warnings': [], 'notes': []}
    if places.data_as_of != report['data_as_of'] or lighting.data_as_of != report['data_as_of']:
        out['errors'].append('pack headers disagree with the report on dataAsOf')
    errs, notes = check_counts(report, sources_report, previous)
    if a.allow_count_change:
        out['warnings'] += [f'(allowed) {e}' for e in errs]
    else:
        out['errors'] += errs
    out['notes'] += notes
    errs, warns, n = check_sentinels(places, _load(a.sentinels), a.partial)
    out['errors'] += errs
    out['warnings'] += warns
    out['sentinels_checked'] = n
    regions_cfg = regions.load(a.regions)
    errs, notes = check_coverage(report, _load(a.known), places, lighting, a.uk_outline, a.partial, regions_cfg)
    errs2, out['strict'] = check_strict(report, places, lighting, a.uk_outline, a.partial, regions_cfg)
    errs += errs2
    if a.partial:
        out['warnings'] += [f'(partial build) {e}' for e in errs]
    else:
        out['errors'] += errs
    out['notes'] += notes
    out['ok'] = not out['errors']
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--report', required=True)
    ap.add_argument('--places-pack', required=True)
    ap.add_argument('--lighting-pack', required=True)
    ap.add_argument('--sources-report')
    ap.add_argument('--previous')
    ap.add_argument('--sentinels', default=os.path.join(ROOT, 'config', 'sentinels.json'))
    ap.add_argument('--known', default=os.path.join(ROOT, 'config', 'known_incomplete.json'))
    ap.add_argument('--uk-outline', default=os.path.join(ROOT, 'config', 'uk_outline.json'))
    ap.add_argument('--regions', default=os.path.join(ROOT, 'config', 'regions.json'))
    ap.add_argument('--allow-count-change', action='store_true')
    ap.add_argument('--partial', action='store_true')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    res = run(a)
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
    print(text)
    sys.exit(0 if res['ok'] else 1)


if __name__ == '__main__':
    main()
