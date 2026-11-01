"""Region boxes (the backend's covers() boxes) and the lighting tile mask.

config/regions.json lists each city box as [minLat, maxLat, minLng, maxLng]
(the order of the backend's covers() checks). Lighting for box-mode extracts
is kept for every tile that meets a box widened by margin_km.
"""
import json
import math
from collections import defaultdict

from pipeline.tiles import box_tiles, to_e7

KM_PER_DEG = 111.32


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def widen(box, margin_km):
    """[minLat, maxLat, minLng, maxLng] widened by margin_km on every side."""
    min_lat, max_lat, min_lng, max_lng = box
    dlat = margin_km / KM_PER_DEG
    mid = math.radians((min_lat + max_lat) / 2)
    dlng = margin_km / (KM_PER_DEG * math.cos(mid))
    return [min_lat - dlat, max_lat + dlat, min_lng - dlng, max_lng + dlng]


def box_e7(box):
    """(min_x, min_y, max_x, max_y) in 1e-7 degree from [minLat, maxLat, minLng, maxLng]."""
    min_lat, max_lat, min_lng, max_lng = box
    return to_e7(min_lng), to_e7(min_lat), to_e7(max_lng), to_e7(max_lat)


def region_tiles(region, widened=True):
    box = widen(region['box'], region.get('margin_km', 0)) if widened else region['box']
    return box_tiles(*box_e7(box))


def lighting_mask(regions_cfg):
    """{row: set(tx)} of every tile meeting any widened region box."""
    mask = defaultdict(set)
    for r in regions_cfg['regions']:
        for tx, ty in region_tiles(r, widened=True):
            mask[ty].add(tx)
    return dict(mask)


def in_box(box, x, y):
    """Whether (x, y) in 1e-7 degree lies in the closed [minLat, maxLat, minLng, maxLng] box."""
    min_x, min_y, max_x, max_y = box_e7(box)
    return min_x <= x <= max_x and min_y <= y <= max_y
