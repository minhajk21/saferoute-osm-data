"""The pack file format (DESIGN.md section 3), writer and reader.

Little-endian throughout.

  header      64 bytes:
                0  magic        8s  b'SROSMPK1'
                8  kind         u8  1 lighting, 2 places
                9  version      u8  1
               10  flags        u16 0
               12  tile_size    u32 200000 (1e-7 degree)
               16  data_as_of   i64 unix seconds, the oldest source timestamp
               24  built_at     i64 unix seconds
               32  index_off    u32    36  index_count  u32
               40  cov_off      u32    44  cov_len      u32
               48  str_off      u32    52  str_len      u32
               56  reserved     8 bytes, zero
  payloads    raw deflate, one per tile, in index order
  strings     u32 count, then per string: u16 byte length, UTF-8 bytes
  index       index_count x 20-byte records (x i32, y i32, offset u32,
              length u32, count u32), sorted by (y, x); offset is absolute
  coverage    u32 row count, then per row: y i32, n u32, n x (x0 i32, x1 i32)
              inclusive runs of COMPLETE tiles, rows by y, runs by x0. A
              complete tile with no index record is a true empty; a tile that
              is not complete must be answered elsewhere (Overpass).

Lighting payload: varint way count, then per way (ids ascending): varint id
delta (from the previous way in the tile, the first from 0), varint index
into the strings (the raw `lit` value), varint point count, then each point
as zigzag-varint (dlon, dlat) at 1e-7 degree, the first relative to the
tile's south-west corner, each next one to the point before.

Places payload: UTF-8 JSON array of Overpass `out center tags` elements in
Overpass order (nodes, ways, relations; each by id): {type, id, lat, lon,
tags} for nodes, {type, id, center: {lat, lon}, tags} otherwise.
"""
import json
import struct
import zlib

MAGIC = b'SROSMPK1'
VERSION = 1
KIND_LIGHTING = 1
KIND_PLACES = 2
HEADER = struct.Struct('<8sBBHIqqIIIIII8x')
INDEX_REC = struct.Struct('<iiIII')
assert HEADER.size == 64 and INDEX_REC.size == 20


def varint(n, buf):
    if n < 0:
        raise ValueError('varint of a negative number')
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)


def zigzag(n):
    return (n << 1) if n >= 0 else ((-n << 1) - 1)


def unzigzag(z):
    return (z >> 1) if not (z & 1) else -((z + 1) >> 1)


def read_varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def deflate_raw(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def inflate_raw(data):
    return zlib.decompress(data, -15)


def encode_lighting_tile(tx, ty, ways, tile_size):
    """ways: [(id, lit_index, xy flat ints)] sorted by id."""
    buf = bytearray()
    varint(len(ways), buf)
    prev = 0
    ox, oy = tx * tile_size, ty * tile_size
    for wid, li, xy in ways:
        varint(wid - prev, buf)
        prev = wid
        varint(li, buf)
        n = len(xy) // 2
        varint(n, buf)
        px, py = ox, oy
        for i in range(n):
            x, y = xy[2 * i], xy[2 * i + 1]
            varint(zigzag(x - px), buf)
            varint(zigzag(y - py), buf)
            px, py = x, y
    return bytes(buf)


def decode_lighting_tile(tx, ty, raw, tile_size, strings):
    pos = 0
    n, pos = read_varint(raw, pos)
    out = []
    prev = 0
    ox, oy = tx * tile_size, ty * tile_size
    for _ in range(n):
        d, pos = read_varint(raw, pos)
        prev += d
        li, pos = read_varint(raw, pos)
        npts, pos = read_varint(raw, pos)
        px, py = ox, oy
        xy = []
        for _ in range(npts):
            zx, pos = read_varint(raw, pos)
            zy, pos = read_varint(raw, pos)
            px += unzigzag(zx)
            py += unzigzag(zy)
            xy += (px, py)
        out.append((prev, strings[li], xy))
    if pos != len(raw):
        raise ValueError('trailing bytes in a lighting tile')
    return out


def encode_places_tile(elements):
    return json.dumps(elements, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


class PackWriter:
    """Streams payloads to disk; tables and the header are written by close()."""

    def __init__(self, path, kind, data_as_of, built_at, tile_size=200_000):
        self.f = open(path, 'wb')
        self.f.write(b'\0' * HEADER.size)
        self.kind, self.data_as_of, self.built_at, self.tile_size = kind, data_as_of, built_at, tile_size
        self.index = []
        self.last = None

    def add_tile(self, tx, ty, payload, count):
        key = (ty, tx)
        if self.last is not None and key <= self.last:
            raise ValueError('tiles must be added in (y, x) order, once each')
        self.last = key
        comp = deflate_raw(payload)
        off = self.f.tell()
        self.f.write(comp)
        self.index.append((tx, ty, off, len(comp), count))
        return len(comp)

    def close(self, strings, coverage_runs):
        f = self.f
        str_off = f.tell()
        sbuf = bytearray(struct.pack('<I', len(strings)))
        for s in strings:
            b = s.encode('utf-8')
            if len(b) > 0xFFFF:
                raise ValueError('string too long for the table')
            sbuf += struct.pack('<H', len(b)) + b
        f.write(sbuf)
        idx_off = f.tell()
        f.write(b''.join(INDEX_REC.pack(*rec) for rec in self.index))
        cov_off = f.tell()
        cbuf = bytearray(struct.pack('<I', len(coverage_runs)))
        for ry, runs in coverage_runs:
            cbuf += struct.pack('<iI', ry, len(runs))
            for x0, x1 in runs:
                cbuf += struct.pack('<ii', x0, x1)
        f.write(cbuf)
        end = f.tell()
        if end >= 2 ** 32:
            raise ValueError('pack over 4 GiB: offsets are u32')
        f.seek(0)
        f.write(HEADER.pack(MAGIC, self.kind, VERSION, 0, self.tile_size, self.data_as_of, self.built_at,
                            idx_off, len(self.index), cov_off, len(cbuf), str_off, len(sbuf)))
        f.close()
        return end


class Pack:
    """Reads a whole pack into memory (tests, sanity checks, fixtures)."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = f.read()
        (magic, self.kind, self.version, _flags, self.tile_size, self.data_as_of, self.built_at,
         idx_off, idx_count, cov_off, cov_len, str_off, str_len) = HEADER.unpack_from(self.data, 0)
        if magic != MAGIC or self.version != VERSION:
            raise ValueError(f'{path}: not a version {VERSION} pack')
        n = struct.unpack_from('<I', self.data, str_off)[0]
        pos = str_off + 4
        self.strings = []
        for _ in range(n):
            ln = struct.unpack_from('<H', self.data, pos)[0]
            self.strings.append(self.data[pos + 2:pos + 2 + ln].decode('utf-8'))
            pos += 2 + ln
        self.index = {}
        self.order = []
        for i in range(idx_count):
            x, y, off, ln, cnt = INDEX_REC.unpack_from(self.data, idx_off + 20 * i)
            self.index[(x, y)] = (off, ln, cnt)
            self.order.append((y, x))
        if self.order != sorted(self.order):
            raise ValueError(f'{path}: index not sorted by (y, x)')
        rows = struct.unpack_from('<I', self.data, cov_off)[0]
        pos = cov_off + 4
        self.coverage = {}
        for _ in range(rows):
            y, k = struct.unpack_from('<iI', self.data, pos)
            pos += 8
            runs = []
            for _ in range(k):
                runs.append(struct.unpack_from('<ii', self.data, pos))
                pos += 8
            self.coverage[y] = runs
        if pos != cov_off + cov_len:
            raise ValueError(f'{path}: coverage table length mismatch')

    def complete(self, tx, ty):
        for x0, x1 in self.coverage.get(ty, ()):
            if x0 <= tx <= x1:
                return True
        return False

    def complete_count(self):
        return sum(x1 - x0 + 1 for runs in self.coverage.values() for x0, x1 in runs)

    def raw(self, tx, ty):
        rec = self.index.get((tx, ty))
        if rec is None:
            return None
        off, ln, _ = rec
        return inflate_raw(self.data[off:off + ln])

    def lighting(self, tx, ty):
        raw = self.raw(tx, ty)
        return [] if raw is None else decode_lighting_tile(tx, ty, raw, self.tile_size, self.strings)

    def places(self, tx, ty):
        raw = self.raw(tx, ty)
        return [] if raw is None else json.loads(raw.decode('utf-8'))
