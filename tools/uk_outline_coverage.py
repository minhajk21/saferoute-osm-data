#!/usr/bin/env python3
"""Which Geofabrik extracts cover the app's UK outline (design probe).

Usage: python3 -I tools/uk_outline_coverage.py INDEX_V1_JSON UKOutline.swift [STEP_DEG]

Decodes the rings of the iOS UKOutline.swift (Google polylines, 1e-5 deg,
latitude first), samples each ring's inside on a STEP_DEG grid (default 0.02),
and reports, per ring, how many samples each candidate extract polygon
contains and how many no candidate contains.
"""
import json
import re
import sys

CANDIDATES = ['united-kingdom', 'isle-of-man', 'guernsey-jersey',
              'ireland-and-northern-ireland', 'britain-and-ireland']


def decode(poly):
    poly = ''.join(ch for ch in poly if ord(ch) >= 63)
    out, i, lat, lng = [], 0, 0, 0
    vals = []
    while i < len(poly):
        result, shift = 0, 0
        while True:
            b = ord(poly[i]) - 63
            i += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        vals.append(~(result >> 1) if result & 1 else result >> 1)
    for k in range(0, len(vals) - 1, 2):
        lat += vals[k]
        lng += vals[k + 1]
        out.append((lng / 1e5, lat / 1e5))
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


class Geom:
    def __init__(self, geom):
        polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
        self.polys = []
        for p in polys:
            rings = [[(c[0], c[1]) for c in r] for r in p]
            xs = [c[0] for c in rings[0]]
            ys = [c[1] for c in rings[0]]
            self.polys.append((min(xs), max(xs), min(ys), max(ys), rings))

    def contains(self, x, y):
        for x0, x1, y0, y1, rings in self.polys:
            if x0 <= x <= x1 and y0 <= y <= y1 and ring_contains(rings[0], x, y) \
                    and not any(ring_contains(h, x, y) for h in rings[1:]):
                return True
        return False


def main():
    index = json.load(open(sys.argv[1]))
    src = open(sys.argv[2]).read()
    step = float(sys.argv[3]) if len(sys.argv) > 3 else 0.02
    feats = {f['properties']['id']: f for f in index['features']}
    geoms = {c: Geom(feats[c]['geometry']) for c in CANDIDATES}
    rings = [decode(m) for m in re.findall(r'#"""\n(.*?)\n\s*"""#', src, re.S)]
    for n, ring in enumerate(rings):
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        counts = {c: 0 for c in CANDIDATES}
        total = uncovered = 0
        miss_uk_iom_ci = 0
        y = min(ys)
        while y <= max(ys):
            x = min(xs)
            while x <= max(xs):
                if ring_contains(ring, x, y):
                    total += 1
                    hit = [c for c in CANDIDATES if geoms[c].contains(x, y)]
                    for c in hit:
                        counts[c] += 1
                    if not hit:
                        uncovered += 1
                    if not set(hit) & {'united-kingdom', 'isle-of-man', 'guernsey-jersey'}:
                        miss_uk_iom_ci += 1
                x += step
            y += step
        print(f'ring {n}: {len(ring)} pts, lat {min(ys):.2f}-{max(ys):.2f}, lng {min(xs):.2f}-{max(xs):.2f}; '
              f'samples {total}; ' + ', '.join(f'{c}={v}' for c, v in counts.items()) +
              f'; in none={uncovered}; outside UK+IoM+GJ files={miss_uk_iom_ci}')


if __name__ == '__main__':
    main()
