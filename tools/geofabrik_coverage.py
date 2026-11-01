#!/usr/bin/env python3
"""Which Geofabrik extracts fully contain each SafeRoute region (design probe).

Usage: python3 -I tools/geofabrik_coverage.py INDEX_V1_JSON REGIONS_JSON

Reads Geofabrik's index-v1.json (extract polygons) and regions.json (the
backend's covers() boxes plus the UK outline's island groups), widens each box
by its margin, samples it on a 0.005 degree grid and prints, per region, the
smallest set of extracts whose polygons contain every sample. Plain Python on
purpose: no geometry library.
"""
import json
import math
import sys


def ring_contains(ring, x, y):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def geom_contains(geom, x, y):
    polys = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
    for poly in polys:
        if ring_contains(poly[0], x, y) and not any(ring_contains(h, x, y) for h in poly[1:]):
            return True
    return False


def widen(box, margin_km):
    min_lat, max_lat, min_lng, max_lng = box
    dlat = margin_km / 111.32
    mid = math.radians((min_lat + max_lat) / 2)
    dlng = margin_km / (111.32 * math.cos(mid))
    return (min_lat - dlat, max_lat + dlat, min_lng - dlng, max_lng + dlng)


def samples(box, step=0.005):
    min_lat, max_lat, min_lng, max_lng = box
    lat = min_lat
    while lat <= max_lat + 1e-9:
        lng = min_lng
        while lng <= max_lng + 1e-9:
            yield lng, lat
            lng += step
        yield max_lng, lat
        lat += step
    lng = min_lng
    while lng <= max_lng + 1e-9:
        yield lng, max_lat
        lng += step


def main():
    index = json.load(open(sys.argv[1]))
    regions = json.load(open(sys.argv[2]))
    feats = {f['properties']['id']: f for f in index['features']}
    candidates = regions['candidates']
    for r in regions['regions']:
        box = widen(r['box'], r.get('margin_km', 0))
        pts = list(samples(box))
        need = {}
        for x, y in pts:
            hits = tuple(c for c in candidates if geom_contains(feats[c]['geometry'], x, y))
            need.setdefault(hits, 0)
            need[hits] += 1
        uncovered = need.get((), 0)
        # Greedy cover: pick the extract that covers most uncovered samples.
        left = [h for h in need if h]
        chosen = []
        covered_sets = {h: n for h, n in need.items() if h}
        while covered_sets:
            score = {}
            for h, n in covered_sets.items():
                for c in h:
                    score[c] = score.get(c, 0) + n
            best = max(score, key=lambda c: (score[c], -len(c)))
            chosen.append(best)
            covered_sets = {h: n for h, n in covered_sets.items() if best not in h}
        print(f"{r['name']:<22} box+{r.get('margin_km', 0)}km "
              f"[{box[0]:.3f},{box[1]:.3f}] x [{box[2]:.3f},{box[3]:.3f}]  "
              f"files={','.join(chosen)}  samples={len(pts)} uncovered={uncovered}")


if __name__ == '__main__':
    main()
