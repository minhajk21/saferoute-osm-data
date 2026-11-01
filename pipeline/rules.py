"""What the packer keeps. Packer side; golden.py restates all of it on its own.

LIGHTING  lighting.js fetchLitSegments(): way[highway][lit](bbox); out geom;
          (backend lighting.js:90-92 at 34ad0d1). Every way with both keys,
          every node of it; the raw `lit` value. The unlit rule
          (UNLIT_VALUES, lighting.js:60) stays in the backend.

PLACES    safePlaces.js tileQuery() (safePlaces.js:419-441 at 34ad0d1):
            nwr  amenity=hospital
            nw   amenity=police
            nw   amenity=pharmacy           (all; is24_7 runs in the backend)
            nw   shop ~ ^(convenience|supermarket)$  and  opening_hours ~ 24/7
          Overpass `~` is an unanchored, case-sensitive POSIX regex search.
          Only the tags the backend reads are kept (PLACE_TAGS).
"""
import re

PLACE_TAGS = (
    'amenity', 'shop', 'opening_hours',                 # classifyType, is24_7
    'emergency', 'beds', 'healthcare', 'healthcare:speciality',
    'wikidata', 'operator', 'brand',                    # hospitalVerdict
    'name', 'name:en', 'alt_name', 'official_name',     # hospitalNames, name
    'phone', 'contact:phone',                           # phone
    'addr:street', 'addr:housenumber', 'addr:city', 'addr:postcode',  # composeAddress
)

SHOP_VALUES = frozenset(('convenience', 'supermarket'))
OPEN_24_7 = re.compile(r'24/7')


def is_lit_way(tags):
    return 'highway' in tags and 'lit' in tags


def is_place(kind, tags):
    """kind is 'n', 'w' or 'r'. `tags` supports `in` and .get()."""
    amenity = tags.get('amenity')
    if amenity == 'hospital':
        return True
    if kind == 'r':
        return False
    if amenity in ('police', 'pharmacy'):
        return True
    shop = tags.get('shop')
    if shop in SHOP_VALUES:
        hours = tags.get('opening_hours')
        return hours is not None and OPEN_24_7.search(hours) is not None
    return False


def kept_tags(tags):
    out = {}
    for k in PLACE_TAGS:
        v = tags.get(k)
        if v is not None:
            out[k] = v
    return out
