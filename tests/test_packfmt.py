"""Pack format: round trips, and the packer's reader agrees with golden.py's independent one."""
import os
import random
import tempfile
import unittest

from tests import helpers  # noqa: F401
from pipeline import golden, packfmt

T = 200_000


class VarintTest(unittest.TestCase):
    def test_round_trip(self):
        for n in [0, 1, 127, 128, 300, 2**31, 2**40 + 5]:
            buf = bytearray()
            packfmt.varint(n, buf)
            self.assertEqual(packfmt.read_varint(buf, 0), (n, len(buf)))
        for z in [0, -1, 1, -2, 2, -(2**31), 2**31 - 1]:
            self.assertEqual(packfmt.unzigzag(packfmt.zigzag(z)), z)
        self.assertEqual([packfmt.zigzag(v) for v in (0, -1, 1, -2, 2)], [0, 1, 2, 3, 4])


class PackTest(unittest.TestCase):
    def test_lighting_and_places_round_trip(self):
        rnd = random.Random(3)
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, 'lighting.pack')
            w = packfmt.PackWriter(lp, packfmt.KIND_LIGHTING, data_as_of=1793395491, built_at=1793400000)
            strings = ['24/7', 'no', 'yes']
            expected = {}
            for ty in (-3, 0, 2575):
                for tx in (-7, 5):
                    ways = []
                    wid = 0
                    for _ in range(rnd.randint(1, 5)):
                        wid += rnd.randint(1, 10**9)
                        xy = []
                        for _ in range(rnd.randint(1, 6)):
                            xy += [tx * T + rnd.randint(-T, 2 * T), ty * T + rnd.randint(-T, 2 * T)]
                        ways.append((wid, rnd.randrange(3), xy))
                    w.add_tile(tx, ty, packfmt.encode_lighting_tile(tx, ty, ways, T), len(ways))
                    expected[(tx, ty)] = [(i, strings[li], xy) for i, li, xy in ways]
            cov = [(-3, [(-7, -7), (5, 9)]), (0, [(-7, 5)]), (2575, [(-7, -7), (5, 5)])]
            w.close(strings, cov)

            pk = packfmt.Pack(lp)
            self.assertEqual((pk.kind, pk.data_as_of, pk.built_at, pk.tile_size), (1, 1793395491, 1793400000, T))
            self.assertTrue(pk.complete(8, -3) and pk.complete(0, 0) and not pk.complete(6, 2575))
            self.assertEqual(pk.complete_count(), 1 + 5 + 13 + 1 + 1)
            gr = golden.PackReader(lp)
            self.assertEqual(len(gr.complete), pk.complete_count())
            for t, ways in expected.items():
                self.assertEqual(pk.lighting(*t), ways)
                raw, cnt = gr.payload(t)
                self.assertEqual(golden.decode_lit(t, raw, gr.strings), [(i, v, list(xy)) for i, v, xy in ways])
                self.assertEqual(cnt, len(ways))
            self.assertIsNone(gr.payload((99, 99)))

            pp = os.path.join(d, 'places.pack')
            w = packfmt.PackWriter(pp, packfmt.KIND_PLACES, 1, 2)
            els = [{'type': 'node', 'id': 5, 'lat': 51.5, 'lon': -0.1, 'tags': {'name': 'Café ☕', 'amenity': 'pharmacy'}}]
            w.add_tile(-5, 2575, packfmt.encode_places_tile(els), 1)
            w.close([], [(2575, [(-5, -5)])])
            self.assertEqual(packfmt.Pack(pp).places(-5, 2575), els)

    def test_order_enforced(self):
        with tempfile.TemporaryDirectory() as d:
            w = packfmt.PackWriter(os.path.join(d, 'x.pack'), 1, 0, 0)
            w.add_tile(5, 1, b'\0', 0)
            with self.assertRaises(ValueError):
                w.add_tile(4, 1, b'\0', 0)
            w.add_tile(4, 2, b'\0', 0)
            w.close([], [])


if __name__ == '__main__':
    unittest.main()
