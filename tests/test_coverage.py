"""Coverage: exact, never claims a tile the polygons do not wholly cover."""
import math
import random
import unittest

from tests import helpers
from pipeline import coverage, tiles

T = tiles.TILE


def poly(geom, mask=None):
    return coverage.Polygon(coverage.geometry_edges(geom), mask)


def ring_geom(pts):
    return {'type': 'Polygon', 'coordinates': [[[helpers.deg(x), helpers.deg(y)] for x, y in pts + [pts[0]]]]}


def flat(cov):
    return {(tx, ty) for ty, xs in cov.items() for tx in xs}


class CoverageTest(unittest.TestCase):
    def test_aligned_square(self):
        g = helpers.square(2 * T, 3 * T, 6 * T, 5 * T)
        self.assertEqual(flat(coverage.complete_tiles([poly(g)])),
                         {(tx, ty) for tx in range(2, 6) for ty in range(3, 5)})

    def test_partial_tiles_are_not_complete(self):
        g = helpers.square(2 * T + 1, 3 * T, 6 * T, 5 * T - 1)
        self.assertEqual(flat(coverage.complete_tiles([poly(g)])),
                         {(tx, 3) for tx in range(3, 6)})

    def test_hole(self):
        outer = [(0, 0), (6 * T, 0), (6 * T, 6 * T), (0, 6 * T)]
        hole = [(2 * T + 7, 2 * T + 7), (3 * T - 7, 2 * T + 7), (3 * T - 7, 3 * T - 7), (2 * T + 7, 3 * T - 7)]
        g = {'type': 'Polygon', 'coordinates': [[[helpers.deg(x), helpers.deg(y)] for x, y in outer + [outer[0]]],
                                                [[helpers.deg(x), helpers.deg(y)] for x, y in hole + [hole[0]]]]}
        got = flat(coverage.complete_tiles([poly(g)]))
        self.assertNotIn((2, 2), got)
        self.assertEqual(len(got), 35)

    def test_thin_overlap_union_is_complete_and_thin_gap_is_not(self):
        # Two polygons meeting along a slanted border that crosses many tiles,
        # overlapping by 2 units (0.2 m) - the DC/Maryland case, narrower.
        a = [(0, 0), (5 * T + 2, 0), (3 * T + 2, 4 * T), (0, 4 * T)]
        b = [(5 * T, 0), (8 * T, 0), (8 * T, 4 * T), (3 * T, 4 * T)]
        got = flat(coverage.complete_tiles([poly(ring_geom(a)), poly(ring_geom(b))]))
        self.assertEqual(got, {(tx, ty) for tx in range(8) for ty in range(4)})
        # now 2 units apart: exactly the tiles the 2-unit gap runs through are not complete
        b_gap = [(5 * T + 4, 0), (8 * T, 0), (8 * T, 4 * T), (3 * T + 4, 4 * T)]
        got = flat(coverage.complete_tiles([poly(ring_geom(a)), poly(ring_geom(b_gap))]))
        seam = {(4, 0), (5, 0), (4, 1), (3, 2), (4, 2), (3, 3)}
        self.assertEqual(got, {(tx, ty) for tx in range(8) for ty in range(4)} - seam)

    def test_mask(self):
        g = helpers.square(0, 0, 4 * T, 4 * T)
        mask = {1: {1, 2}}
        self.assertEqual(flat(coverage.complete_tiles([poly(g, mask)])), {(1, 1), (2, 1)})

    def test_random_polygons_never_overclaim(self):
        rnd = random.Random(11)
        claimed = 0
        for _ in range(25):
            rings = []
            for _ in range(rnd.randint(1, 3)):
                cx, cy = rnd.randint(2 * T, 6 * T), rnd.randint(2 * T, 6 * T)
                pts = []
                for k in range(rnd.randint(3, 7)):
                    ang = 2 * math.pi * k / 7 + rnd.random() * 0.3
                    r = rnd.randint(T, 3 * T)
                    pts.append((cx + int(r * math.cos(ang)), cy + int(r * math.sin(ang))))
                rings.append(pts)
            got = flat(coverage.complete_tiles([poly(ring_geom(p)) for p in rings]))
            claimed += len(got)
            for tx, ty in got:
                for i in range(9):
                    for j in range(9):
                        x, y = tx * T + i * T // 8, ty * T + j * T // 8
                        # nudge samples off exact vertices/edges of the tile grid
                        x += 1 if i == 0 else (-1 if i == 8 else 0)
                        y += 1 if j == 0 else (-1 if j == 8 else 0)
                        self.assertTrue(any(_pip(p, x, y) for p in rings), (tx, ty, x, y))
        self.assertGreater(claimed, 50)


def _pip(pts, x, y):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        (xi, yi), (xj, yj) = pts[i], pts[j]
        if (yi > y) != (yj > y):
            dy = yj - yi
            lhs, rhs = (x - xi) * dy, (xj - xi) * (y - yi)
            if (lhs < rhs) if dy > 0 else (lhs > rhs):
                inside = not inside
        j = i
    return inside


if __name__ == '__main__':
    unittest.main()
