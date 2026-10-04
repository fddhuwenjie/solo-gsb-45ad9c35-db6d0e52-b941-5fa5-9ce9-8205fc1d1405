"""SQLite 连接与建表。"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "correlation.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS borehole (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    x           REAL    NOT NULL DEFAULT 0,
    collar_elev REAL    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    borehole_id INTEGER NOT NULL REFERENCES borehole(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    top        REAL    NOT NULL,          -- 层顶深度（自孔口向下，米）
    bottom     REAL    NOT NULL,          -- 层底深度（自孔口向下，米）
    lithology  TEXT    NOT NULL,
    grain      TEXT    NOT NULL DEFAULT '未知',
    water      TEXT    NOT NULL DEFAULT '未知',
    confidence REAL    NOT NULL DEFAULT 1.0,
    UNIQUE (borehole_id, seq)
);

CREATE TABLE IF NOT EXISTS marker (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS marker_layer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id  INTEGER NOT NULL REFERENCES marker(id) ON DELETE CASCADE,
    borehole_id INTEGER NOT NULL REFERENCES borehole(id) ON DELETE CASCADE,
    layer_id   INTEGER NOT NULL REFERENCES layer(id) ON DELETE CASCADE,
    UNIQUE (marker_id, borehole_id)
);

CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS run_result (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT,
    generated_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "weights": {
        "lithology": 0.45,
        "grain": 0.15,
        "water": 0.10,
        "elev": 0.20,
        "confidence": 0.10,
    },
    "elev_scale": 5.0,      # 高程差特征尺度（米），差 5m 时高程相似度≈0.37
    "gap_open": 2.0,        # 开间隙惩罚（相似度单位）
    "gap_extend": 1.0,      # 续间隙惩罚
}


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        import json
        row = conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (id, payload) VALUES (1, ?)",
                (json.dumps(DEFAULT_SETTINGS, ensure_ascii=False),),
            )
        conn.commit()
    finally:
        conn.close()
