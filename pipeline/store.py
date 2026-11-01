"""SQLite intermediate store (stdlib) between the per-extract passes and the packer.

One store per build. Each extract adds its rows; an element found in two
extracts keeps the higher version (then the newer extract). Coordinates are
OSM's 1e-7 degree integers, stored as little-endian int32 pairs.
"""
import json
import sqlite3
import sys
from array import array

if sys.byteorder != 'little':
    raise RuntimeError('the store and the packs assume a little-endian host')

KIND = {'n': 0, 'w': 1, 'r': 2}
KIND_NAME = {0: 'node', 1: 'way', 2: 'relation'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (id TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lit_way (
    id INTEGER PRIMARY KEY, version INTEGER NOT NULL, ts INTEGER NOT NULL, src TEXT NOT NULL,
    lit TEXT NOT NULL, xy BLOB NOT NULL, missing INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS place (
    kind INTEGER NOT NULL, id INTEGER NOT NULL, version INTEGER NOT NULL, ts INTEGER NOT NULL,
    src TEXT NOT NULL, tags TEXT NOT NULL, xy BLOB, missing INTEGER NOT NULL, members TEXT,
    PRIMARY KEY (kind, id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS member_node (
    id INTEGER PRIMARY KEY, version INTEGER NOT NULL, ts INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS member_way (
    id INTEGER PRIMARY KEY, version INTEGER NOT NULL, ts INTEGER NOT NULL, xy BLOB NOT NULL, missing INTEGER NOT NULL);
"""

_NEWER = "WHERE excluded.version > {t}.version OR (excluded.version = {t}.version AND excluded.ts > {t}.ts)"

UPSERT = {
    'lit_way': ("INSERT INTO lit_way (id, version, ts, src, lit, xy, missing) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, ts=excluded.ts, src=excluded.src, "
                "lit=excluded.lit, xy=excluded.xy, missing=excluded.missing " + _NEWER.format(t='lit_way')),
    'place': ("INSERT INTO place (kind, id, version, ts, src, tags, xy, missing, members) VALUES (?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(kind, id) DO UPDATE SET version=excluded.version, ts=excluded.ts, src=excluded.src, "
              "tags=excluded.tags, xy=excluded.xy, missing=excluded.missing, members=excluded.members "
              + _NEWER.format(t='place')),
    'member_node': ("INSERT INTO member_node (id, version, ts, x, y) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, ts=excluded.ts, x=excluded.x, y=excluded.y "
                    + _NEWER.format(t='member_node')),
    'member_way': ("INSERT INTO member_way (id, version, ts, xy, missing) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET version=excluded.version, ts=excluded.ts, xy=excluded.xy, "
                   "missing=excluded.missing " + _NEWER.format(t='member_way')),
}


def open_store(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=OFF')
    db.execute('PRAGMA synchronous=OFF')
    db.executescript(SCHEMA)
    return db


def pack_xy(flat):
    return array('i', flat).tobytes()


def unpack_xy(blob):
    a = array('i')
    a.frombytes(blob)
    return a


class Batch:
    """Buffered upserts into one table."""

    def __init__(self, db, table, size=20_000):
        self.db, self.sql, self.size, self.rows, self.count = db, UPSERT[table], size, [], 0

    def add(self, row):
        self.rows.append(row)
        self.count += 1
        if len(self.rows) >= self.size:
            self.flush()

    def flush(self):
        if self.rows:
            self.db.executemany(self.sql, self.rows)
            self.rows = []


def put_source(db, source_id, info):
    db.execute('INSERT OR REPLACE INTO source (id, info) VALUES (?, ?)',
               (source_id, json.dumps(info, sort_keys=True)))
    db.commit()


def sources(db):
    return {sid: json.loads(info) for sid, info in db.execute('SELECT id, info FROM source ORDER BY id')}
