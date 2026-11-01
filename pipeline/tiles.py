"""Tile arithmetic in OSM's own integers (1e-7 degree). Packer side.

A tile is the backend's 0.02 degree square (safePlaces.js TILE_SIZE): tile
(x, y) spans [x*T, (x+1)*T] in longitude and [y*T, (y+1)*T] in latitude,
with T = 200,000 units of 1e-7 degree. Tiles are CLOSED, as an Overpass bbox
is: a point on an edge belongs to every tile that edge bounds (DESIGN.md A1,
A6). Everything here is exact integer arithmetic; no floats.

pipeline/golden.py re-implements its own geometry on purpose and must not
import this module.
"""

E7 = 10_000_000
TILE = 200_000          # 0.02 degree


def to_e7(deg):
    """Degrees to 1e-7 degree units, rounded to nearest."""
    return int(round(deg * E7))


def span(lo, hi):
    """(first, last) tile index whose closed interval meets [lo, hi]."""
    a = lo // TILE
    if lo == a * TILE:
        a -= 1          # lo on an edge: the tile below/left touches it too
    return a, hi // TILE


def point_tiles(x, y):
    """Every tile whose closed rectangle contains the point."""
    x0, x1 = span(x, x)
    y0, y1 = span(y, y)
    return [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]


def segment_touches(ax, ay, bx, by, x0, y0, x1, y1):
    """Whether segment AB meets the CLOSED rectangle [x0,x1] x [y0,y1].

    Separating-axis test: the two box axes, then the segment's normal (the
    rectangle lies strictly on one side of the line through AB).
    """
    if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
        return False
    dx, dy = bx - ax, by - ay
    pos = neg = False
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        s = dx * (cy - ay) - dy * (cx - ax)
        if s > 0:
            pos = True
        elif s < 0:
            neg = True
        else:
            return True     # a corner on the line, inside the boxes' overlap
        if pos and neg:
            return True
    return False


def segment_meets_open(ax, ay, bx, by, x0, y0, x1, y1):
    """Whether segment AB meets the OPEN rectangle (x0,x1) x (y0,y1).

    Used by coverage: an edge that only runs along a cell's border leaves the
    cell's interior wholly on one side, so the cell is decided by its centre.
    """
    if max(ax, bx) <= x0 or min(ax, bx) >= x1 or max(ay, by) <= y0 or min(ay, by) >= y1:
        return False
    dx, dy = bx - ax, by - ay
    pos = neg = False
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        s = dx * (cy - ay) - dy * (cx - ax)
        if s > 0:
            pos = True
        elif s < 0:
            neg = True
    return pos and neg


def add_segment_tiles(ax, ay, bx, by, out):
    """Add to `out` every tile whose closed rectangle segment AB touches."""
    tx0, tx1 = span(min(ax, bx), max(ax, bx))
    ty0, ty1 = span(min(ay, by), max(ay, by))
    if tx0 == tx1 and ty0 == ty1:
        out.add((tx0, ty0))
        return
    for ty in range(ty0, ty1 + 1):
        ry0 = ty * TILE
        ry1 = ry0 + TILE
        for tx in range(tx0, tx1 + 1):
            rx0 = tx * TILE
            if segment_touches(ax, ay, bx, by, rx0, ry0, rx0 + TILE, ry1):
                out.add((tx, ty))


def path_tiles(xy):
    """Tiles a way touches. `xy` is a flat sequence x0, y0, x1, y1, ...

    A one-node way is a point. An empty sequence touches nothing.
    """
    out = set()
    n = len(xy) // 2
    if n == 0:
        return out
    if n == 1:
        out.update(point_tiles(xy[0], xy[1]))
        return out
    ax, ay = xy[0], xy[1]
    for i in range(1, n):
        bx, by = xy[2 * i], xy[2 * i + 1]
        add_segment_tiles(ax, ay, bx, by, out)
        ax, ay = bx, by
    return out


def box_tiles(min_x, min_y, max_x, max_y):
    """Every tile whose closed rectangle meets the closed box."""
    tx0, tx1 = span(min_x, max_x)
    ty0, ty1 = span(min_y, max_y)
    return [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
