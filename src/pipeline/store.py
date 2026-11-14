"""Durable immutable inputs and separately mutable processing state."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..common.canonical import canonical_sha256
from ..common.models import ProtocolError, utc_iso


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, input TEXT NOT NULL,
                digest TEXT NOT NULL, state TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def insert(self, kind, key, payload, state=None):
        digest = canonical_sha256(payload)
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT digest FROM objects WHERE kind=? AND id=?", (kind,key)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ProtocolError('IDEMPOTENCY_CONFLICT', 'same ID with different original content', object_id=key)
                return False
            now = utc_iso()
            conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)", (kind,key,json.dumps(payload,ensure_ascii=False),digest,json.dumps(state or {}),now,now))
            return True

    def update(self, kind, key, **values):
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT state FROM objects WHERE kind=? AND id=?", (kind,key)).fetchone()
            if not row:
                raise ProtocolError('NOT_FOUND','object not found',object_id=key)
            state = json.loads(row['state'])
            state.update(values)
            conn.execute("UPDATE objects SET state=?,updated=? WHERE kind=? AND id=?", (json.dumps(state,ensure_ascii=False),utc_iso(),kind,key))

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        return {'id':row['id'],'payload':json.loads(row['input']),'state':json.loads(row['state']),'createdAt':row['created'],'updatedAt':row['updated']}

    def get(self, kind, key):
        with self.connect() as conn:
            return self.unpack(conn.execute("SELECT * FROM objects WHERE kind=? AND id=?",(kind,key)).fetchone())

    def all(self, kind, omit=()):
        with self.connect() as conn:
            if omit:
                placeholders=','.join('?' for _ in omit)
                query=f'SELECT kind,id,json_remove(input,{placeholders}) AS input,digest,state,created,updated FROM objects WHERE kind=? ORDER BY rowid'
                rows=conn.execute(query,tuple('$.'+key for key in omit)+(kind,))
            else:
                rows=conn.execute('SELECT * FROM objects WHERE kind=? ORDER BY rowid',(kind,))
            return [self.unpack(row) for row in rows]

    def setting(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.lock, self.connect() as conn:
            conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))
