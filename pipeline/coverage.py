"""Which tiles lie wholly inside the union of the extracts' polygons.

A tile is COMPLETE when every point of it is inside at least one downloaded
extract's clipping polygon (Geofabrik's .poly, which index-v1.json carries):
only then does the extract hold every element that touches it. The test is
exact, in integers and rationals at 1e-7 degree:

  * a tile whose interior no edge of polygon P meets is wholly inside P or
    wholly outside it, and its centre says which (ray cast);
  * a tile inside some polygon is complete;
  * a tile that edges cross (a coast, or a border between two extracts) is
    cut into horizontal slabs at every y where the picture can change (edge
    end points, edge-edge crossings, edges meeting the tile's sides); it is
    complete only if, in every slab, the polygons' inside intervals cover the
    tile's whole width. Neighbouring US state polygons overlap by only 20-100
    m (measured along DC's border), so a sampling or cell-subdivision test
    would wrongly call every border tile incomplete or, worse, complete.

Box-mode extracts (lighting clipped to the city boxes) take part only for the
tiles in their mask: a tile outside every box is not covered by them.
"""
import math
from collections import defaultdict
from fractions import Fraction

from pipeline.tiles import TILE, span, segment_meets_open, segment_touches, to_e7

HALF = TILE // 2


def geometry_edges(geometry):
    """Integer edges (ax, ay, bx, by) of a GeoJSON Polygon/MultiPolygon."""
    if geometry['type'] == 'Polygon':
        polys = [geometry['coordinates']]
    elif geometry['type'] == 'MultiPolygon':
        polys = geometry['coordinates']
    else:
        raise ValueError(f"unsupported geometry {geometry['type']}")
    edges = []
    for poly in polys:
        for ring in poly:
            pts = [(to_e7(p[0]), to_e7(p[1])) for p in ring]
            if pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if (ax, ay) != (bx, by):
                    edges.append((ax, ay, bx, by))
    return edges


class Polygon:
    """One extract polygon, its edges bucketed by tile row."""

    def __init__(self, edges, mask=None):
        if not edges:
            raise ValueError('polygon has no edges')
        self.edges = edges
        self.mask = mask        # None, or {row: set(tx)} of tiles it may cover
        self.rows = defaultdict(list)
        xs, ys = [], []
        for e in edges:
            ax, ay, bx, by = e
            r0, r1 = span(min(ay, by), max(ay, by))
            for r in range(r0, r1 + 1):
                self.rows[r].append(e)
            xs += (ax, bx)
            ys += (ay, by)
        self.col_range = span(min(xs), max(xs))
        self.row_range = span(min(ys), max(ys))

    def allowed(self, ry, tx):
        return self.mask is None or tx in self.mask.get(ry, ())


def _x_at(e, y):
    """x where edge e crosses latitude y, as a Fraction (e not horizontal)."""
    ax, ay, bx, by = e
    return ax + Fraction((bx - ax) * (y - ay), by - ay)


def point_inside(bucket, px, py):
    """Even-odd ray cast to +x. `bucket` holds every edge crossing y = py."""
    inside = False
    for ax, ay, bx, by in bucket:
        if (ay > py) != (by > py):
            dy = by - ay
            lhs = (px - ax) * dy
            rhs = (bx - ax) * (py - ay)
            if (lhs < rhs) if dy > 0 else (lhs > rhs):
                inside = not inside
    return inside


def _row_scan(poly, ry):
    """(crossed, inside) tile-x sets of one tile row for one polygon."""
    y0 = ry * TILE
    y1 = y0 + TILE
    bucket = poly.rows.get(ry, ())
    crossed = set()
    for e in bucket:
        ax, ay, bx, by = e
        if ay == by:
            lo, hi = min(ax, bx), max(ax, bx)
        else:
            ylo, yhi = max(min(ay, by), y0), min(max(ay, by), y1)
            xa, xb = _x_at(e, ylo), _x_at(e, yhi)
            lo, hi = math.floor(min(xa, xb)), math.ceil(max(xa, xb))
        c0, c1 = span(lo, hi)
        for tx in range(c0, c1 + 1):
            x0 = tx * TILE
            if segment_meets_open(ax, ay, bx, by, x0, y0, x0 + TILE, y1):
                crossed.add(tx)
    yc = y0 + HALF
    xs = sorted(_x_at(e, yc) for e in bucket if (e[1] > yc) != (e[3] > yc))
    inside = set()
    for a, b in zip(xs[0::2], xs[1::2]):
        first = math.floor((a - HALF) / TILE) + 1      # centre > a
        last = math.ceil((b - HALF) / TILE) - 1        # centre < b
        for tx in range(first, last + 1):
            if tx not in crossed:
                inside.add(tx)
    return crossed, inside


def _cross_y(e, f):
    """y of the single crossing point of segments e and f, or None."""
    ax, ay, bx, by = e
    cx, cy, dx, dy = f
    rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
    den = rx * sy - ry * sx
    if den == 0:
        return None         # parallel; overlapping runs change things only at end points
    tn = (cx - ax) * sy - (cy - ay) * sx
    un = (cx - ax) * ry - (cy - ay) * rx
    if den < 0:
        den, tn, un = -den, -tn, -un
    if not (0 <= tn <= den and 0 <= un <= den):
        return None
    return ay + Fraction(tn * ry, den)


def _intervals(bucket, y):
    """Inside intervals [(a, b)] of one polygon on the line at y (no vertex on it)."""
    xs = sorted(_x_at(e, y) for e in bucket if (e[1] > y) != (e[3] > y))
    return list(zip(xs[0::2], xs[1::2]))


def tile_covered(x0, y0, x1, y1, buckets):
    """Whether the closed tile lies in the union of polygons given by row buckets."""
    near = []
    events = {Fraction(y0), Fraction(y1)}
    for bucket in buckets:
        for e in bucket:
            if not segment_touches(*e, x0, y0, x1, y1):
                continue
            near.append(e)
            ax, ay, bx, by = e
            for v in (ay, by):
                if y0 < v < y1:
                    events.add(Fraction(v))
            for side in (x0, x1):
                yv = _cross_y(e, (side, y0, side, y1))
                if yv is not None and y0 < yv < y1:
                    events.add(yv)
    for i in range(len(near)):
        for j in range(i + 1, len(near)):
            yv = _cross_y(near[i], near[j])
            if yv is not None and y0 < yv < y1:
                events.add(yv)
    ys = sorted(events)
    for ya, yb in zip(ys, ys[1:]):
        ym = (ya + yb) / 2
        ivs = sorted(iv for bucket in buckets for iv in _intervals(bucket, ym) if iv[1] >= x0 and iv[0] <= x1)
        reach = Fraction(x0)
        for a, b in ivs:
            if a > reach:
                return False
            if b > reach:
                reach = b
            if reach >= x1:
                break
        if reach < x1:
            return False
    return True


def complete_tiles(polygons):
    """{row: set(tx)} of tiles wholly inside the union of `polygons`."""
    covered = defaultdict(set)
    pending = defaultdict(lambda: defaultdict(list))
    for pi, poly in enumerate(polygons):
        for ry in range(poly.row_range[0], poly.row_range[1] + 1):
            if poly.mask is not None and ry not in poly.mask:
                continue
            crossed, inside = _row_scan(poly, ry)
            for tx in inside:
                if poly.allowed(ry, tx):
                    covered[ry].add(tx)
            for tx in crossed:
                if poly.allowed(ry, tx):
                    pending[ry][tx].append(pi)
    for ry, cols in pending.items():
        y0 = ry * TILE
        row = covered[ry]
        for tx, pis in cols.items():
            if tx in row:
                continue
            x0 = tx * TILE
            if tile_covered(x0, y0, x0 + TILE, y0 + TILE, [polygons[pi].rows[ry] for pi in pis]):
                row.add(tx)
    return {ry: cols for ry, cols in covered.items() if cols}


def to_runs(tiles_by_row):
    """{row: set(tx)} -> sorted [(ry, [(x0, x1), ...])], runs inclusive."""
    out = []
    for ry in sorted(tiles_by_row):
        xs = sorted(tiles_by_row[ry])
        runs = []
        for tx in xs:
            if runs and tx == runs[-1][1] + 1:
                runs[-1][1] = tx
            else:
                runs.append([tx, tx])
        out.append((ry, [tuple(r) for r in runs]))
    return out


def count(tiles_by_row):
    return sum(len(v) for v in tiles_by_row.values())


def touched_tiles(poly):
    """{row: set(tx)} of tiles a polygon's interior or edges meet (for checks)."""
    out = defaultdict(set)
    for ry in range(poly.row_range[0], poly.row_range[1] + 1):
        crossed, inside = _row_scan(poly, ry)
        out[ry] |= crossed | inside
    return {ry: cols for ry, cols in out.items() if cols}


def decode_polyline(text):
    """Google encoded polyline (1e-5 degree, latitude first) -> [(lng, lat)]."""
    text = ''.join(ch for ch in text if ord(ch) >= 63)
    vals, i = [], 0
    while i < len(text):
        result = shift = 0
        while True:
            b = ord(text[i]) - 63
            i += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        vals.append(~(result >> 1) if result & 1 else result >> 1)
    out, lat, lng = [], 0, 0
    for k in range(0, len(vals) - 1, 2):
        lat += vals[k]
        lng += vals[k + 1]
        out.append((lng / 1e5, lat / 1e5))
    return out
