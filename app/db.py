"""SQLite storage for the collection inventory."""
import sqlite3

from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES locations(id),
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS descriptor_types (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ref                  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    barcode              TEXT,
    name                 TEXT NOT NULL,
    description          TEXT,
    notes                TEXT,
    quantity             INTEGER NOT NULL DEFAULT 1,
    location_id          INTEGER REFERENCES locations(id),
    parent_id            INTEGER REFERENCES items(id),
    harvested_from_id    INTEGER REFERENCES items(id),
    status               TEXT NOT NULL DEFAULT 'active',
    value                REAL,
    purchase_date        TEXT,
    purchase_price       REAL,
    purchase_seller      TEXT,
    purchase_platform    TEXT,
    disposal_date        TEXT,
    disposal_price       REAL,
    disposal_destination TEXT,
    broken_date          TEXT,
    work_cost            REAL NOT NULL DEFAULT 0,
    created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at           TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_items_parent    ON items(parent_id);
CREATE INDEX IF NOT EXISTS ix_items_location  ON items(location_id);
CREATE INDEX IF NOT EXISTS ix_items_harvested ON items(harvested_from_id);
CREATE INDEX IF NOT EXISTS ix_items_barcode   ON items(barcode);
CREATE INDEX IF NOT EXISTS ix_items_status    ON items(status);

CREATE TABLE IF NOT EXISTS item_descriptors (
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    descriptor_type_id INTEGER NOT NULL REFERENCES descriptor_types(id) ON DELETE CASCADE,
    value              TEXT NOT NULL,
    PRIMARY KEY (item_id, descriptor_type_id)
);

CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_photos_item ON photos(item_id);

-- Valuation history. items.value caches the current value worked out from this (see refresh_value).
CREATE TABLE IF NOT EXISTS valuations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    value      REAL NOT NULL,
    source     TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_valuations_item ON valuations(item_id, date);

-- Repairs and other work done on an item. items.work_cost caches the total cost.
CREATE TABLE IF NOT EXISTS work_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    description TEXT NOT NULL,
    cost        REAL,
    part_id     INTEGER REFERENCES items(id) ON DELETE SET NULL,  -- a donor part fitted by this work
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_work_log_item ON work_log(item_id, date);
"""

DEFAULT_SETTINGS = {
    "schema_version": "5",
    "label_format": "qr",
    "public_url": "",
    "ref_prefix": "RC-",
    "ref_digits": "5",
    "ref_next": "1",
    "currency": "£",
    "site_name": "RelicBox",
}

DEFAULT_DESCRIPTORS = ["Manufacturer", "Model", "Serial number", "Version / revision"]


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if conn.execute("SELECT COUNT(*) FROM descriptor_types").fetchone()[0] == 0:
        for i, name in enumerate(DEFAULT_DESCRIPTORS):
            conn.execute("INSERT INTO descriptor_types(name, sort_order) VALUES (?, ?)", (name, i))
    conn.commit()
    conn.close()


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Add columns introduced after the first release (safe to run repeatedly)."""
    # legacy_id: the ID an item/location had in another system (e.g. Homebox UUID), so old QR labels still work.
    for table in ("items", "locations"):
        if "legacy_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN legacy_id TEXT")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_legacy ON {table}(legacy_id)")
    if "work_cost" not in _columns(conn, "items"):
        conn.execute("ALTER TABLE items ADD COLUMN work_cost REAL NOT NULL DEFAULT 0")
    if "part_id" not in _columns(conn, "work_log"):
        conn.execute("ALTER TABLE work_log ADD COLUMN part_id INTEGER REFERENCES items(id) ON DELETE SET NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_work_log_part ON work_log(part_id)")
    # v3: values become a dated history. Keep any single value recorded before that as the first valuation.
    conn.execute("""INSERT INTO valuations(item_id, date, value, source)
                    SELECT id, substr(COALESCE(updated_at, created_at, date('now')), 1, 10), value,
                           'Recorded before value history'
                    FROM items i
                    WHERE value IS NOT NULL AND NOT EXISTS (SELECT 1 FROM valuations v WHERE v.item_id = i.id)""")
    # v4: sold / given-away / scrapped items leave their location (they're listed under "Sold" instead)
    # and remember where they were, so "Back in collection" can put them back.
    for col in ("former_location_id", "former_parent_id"):
        if col not in _columns(conn, "items"):
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} INTEGER")
    move_out_disposed(conn)
    # v5: a part kept as its own item can be given a value when it comes off the donor. That value is
    # fixed income for the donor and the part's own cost.
    if "salvage_value" not in _columns(conn, "items"):
        conn.execute("ALTER TABLE items ADD COLUMN salvage_value REAL")
    conn.execute("UPDATE settings SET value = '5' WHERE key = 'schema_version'")


DISPOSED = ("sold", "given_away", "scrapped")


def move_out_disposed(conn):
    """Take disposed items out of their location, and out of any container that's still in the collection.

    Items that went *with* a disposed container (e.g. a card sold inside a computer) stay inside it.
    Safe to run repeatedly.
    """
    gone = ",".join(f"'{s}'" for s in DISPOSED)
    conn.execute(f"""UPDATE items SET former_location_id = COALESCE(former_location_id, location_id), location_id = NULL
                     WHERE status IN ({gone}) AND location_id IS NOT NULL""")
    conn.execute(f"""UPDATE items SET former_parent_id = COALESCE(former_parent_id, parent_id), parent_id = NULL
                     WHERE status IN ({gone}) AND parent_id IS NOT NULL
                       AND parent_id IN (SELECT id FROM items WHERE status NOT IN ({gone}))""")


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    cur = get_db().execute(sql, args)
    return cur.lastrowid


def commit():
    get_db().commit()
