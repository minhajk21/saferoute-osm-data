"""Tile arithmetic: closed tiles, exact segment tests, cross-checked against golden.py's own geometry."""
import random
import unittest

from tests import helpers  # noqa: F401  (puts the repo on sys.path)
from pipeline import golden, tiles

T = tiles.TILE


class SpanTest(unittest.TestCase):
    def test_interior_and_edges(self):
        self.assertEqual(tiles.span(5, 10), (0, 0))
        self.assertEqual(tiles.span(T, T), (0, 1))            # on an edge: both tiles
        self.assertEqual(tiles.span(0, 0), (-1, 0))
        self.assertEqual(tiles.span(-1, -1), (-1, -1))
        self.assertEqual(tiles.span(-T, -T), (-2, -1))
        self.assertEqual(tiles.span(T - 1, T + 1), (0, 1))

    def test_point_on_corner_is_in_four_tiles(self):
        self.assertEqual(sorted(tiles.point_tiles(3 * T, -2 * T)), sorted([(2, -3), (3, -3), (2, -2), (3, -2)]))
        self.assertEqual(tiles.point_tiles(3 * T + 1, 5), [(3, 0)])

    def test_backend_tile_of_a_point(self):
        # safePlaces.js tile of Trafalgar Square (51.508, -0.128): floor(v / 0.02)
        x, y = tiles.to_e7(-0.128), tiles.to_e7(51.508)
        self.assertEqual(tiles.point_tiles(x, y), [(-7, 2575)])


class SegmentTest(unittest.TestCase):
    def test_crossing_without_nodes_inside(self):
        # from tile (0,0) to tile (2,0): passes through (1,0) with no node in it
        out = set()
        tiles.add_segment_tiles(T // 2, T // 2, 2 * T + T // 2, T // 2, out)
        self.assertEqual(out, {(0, 0), (1, 0), (2, 0)})

    def test_corner_clip(self):
        # passes exactly through the corner (T, T): touches the four tiles there
        out = set()
        tiles.add_segment_tiles(T // 2, T // 2, T + T // 2, T + T // 2, out)
        self.assertEqual(out, {(0, 0), (1, 1), (0, 1), (1, 0)})
        # just misses the corner: not (1, 0)
        out = set()
        tiles.add_segment_tiles(T // 2, T // 2 + 2, T + T // 2, T + T // 2 + 2, out)
        self.assertEqual(out, {(0, 0), (0, 1), (1, 1)})

    def test_along_an_edge(self):
        out = set()
        tiles.add_segment_tiles(10, T, 50, T, out)
        self.assertEqual(out, {(0, 0), (0, 1)})

    def test_random_against_golden_liang_barsky(self):
        rnd = random.Random(7)
        for _ in range(4000):
            # coordinates near tile edges so edge cases are common
            pts = [(rnd.choice([0, T, 2 * T, -T]) + rnd.choice([0, 0, 1, -1, rnd.randint(-T, T)]),
                    rnd.choice([0, T, 2 * T, -T]) + rnd.choice([0, 0, 1, -1, rnd.randint(-T, T)]))
                   for _ in range(rnd.randint(1, 4))]
            flat = [c for p in pts for c in p]
            self.assertEqual(tiles.path_tiles(flat), golden.line_tile_set(pts), pts)

    def test_open_rect(self):
        self.assertFalse(tiles.segment_meets_open(0, 0, T, 0, 0, 0, T, T))   # along the bottom edge
        self.assertTrue(tiles.segment_meets_open(0, 1, T, 1, 0, 0, T, T))
        self.assertFalse(tiles.segment_meets_open(-5, -5, 0, 0, 0, 0, T, T))  # ends on a corner


if __name__ == '__main__':
    unittest.main()
