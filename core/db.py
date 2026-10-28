"""SQLite 数据层：所有项目状态、批次队列、生成结果都落库，保证断点续跑。"""
from __future__ import annotations

import os
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NOVEL_DATA_DIR") or (ROOT / "data"))
DB_PATH = Path(os.environ.get("NOVEL_DB_PATH") or (DATA_DIR / "novel.db"))


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_name TEXT DEFAULT '',
    source_chars INTEGER DEFAULT 0,
    status TEXT DEFAULT 'created',
    meta TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    word_count INTEGER DEFAULT 0,
    batch_idx INTEGER DEFAULT 0,
    UNIQUE(project_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_chapters_project ON chapters(project_id, idx);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    batch_idx INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    updated_at TEXT,
    UNIQUE(project_id, kind, batch_idx)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    content TEXT DEFAULT '',
    updated_at TEXT,
    UNIQUE(project_id, key)
);

CREATE TABLE IF NOT EXISTS outline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL UNIQUE,
    requirement TEXT DEFAULT '',
    content TEXT DEFAULT '',
    status TEXT DEFAULT 'empty',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS constitution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL UNIQUE,
    content TEXT DEFAULT '',
    locked INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS generated (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    polished TEXT DEFAULT '',
    batch_idx INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',
    zhuque_score REAL,
    updated_at TEXT,
    UNIQUE(project_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_generated_project ON generated(project_id, idx);

CREATE TABLE IF NOT EXISTS contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    after_batch INTEGER NOT NULL,
    content TEXT DEFAULT '',
    deviation TEXT DEFAULT '',
    updated_at TEXT,
    UNIQUE(project_id, after_batch)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    type TEXT NOT NULL,
    title TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    progress INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    project_id INTEGER,
    stage TEXT,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    # 某些网络盘 / 同步盘不支持 WAL，逐级回退
    for mode in ("WAL", "TRUNCATE", "DELETE"):
        try:
            c.execute(f"PRAGMA journal_mode={mode}")
            break
        except sqlite3.OperationalError:
            continue
    try:
        c.execute("PRAGMA busy_timeout=10000")
    except sqlite3.OperationalError:
        pass
    return c


def init() -> None:
    with closing(conn()) as c:
        c.executescript(SCHEMA)
        c.commit()


def q(sql: str, args: tuple = ()) -> list[dict]:
    with closing(conn()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with closing(conn()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return int(cur.lastrowid or 0)


def upsert_asset(project_id: int, key: str, content: str) -> None:
    execute(
        """INSERT INTO assets(project_id, key, content, updated_at) VALUES(?,?,?,?)
           ON CONFLICT(project_id, key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
        (project_id, key, content, now()),
    )


def get_asset(project_id: int, key: str, default: str = "") -> str:
    row = q1("SELECT content FROM assets WHERE project_id=? AND key=?", (project_id, key))
    return row["content"] if row else default


def set_batch(project_id: int, kind: str, batch_idx: int, status: str, error: str = "") -> None:
    execute(
        """INSERT INTO batches(project_id, kind, batch_idx, status, error, updated_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(project_id, kind, batch_idx) DO UPDATE SET status=excluded.status,
             error=excluded.error, updated_at=excluded.updated_at""",
        (project_id, kind, batch_idx, status, error, now()),
    )


def create_task(project_id: int | None, type_: str, title: str, payload: dict | None = None,
                total: int = 0) -> int:
    payload_text = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    with closing(conn()) as c:
        c.execute("BEGIN IMMEDIATE")
        if project_id is None:
            old = c.execute(
                "SELECT id FROM tasks WHERE project_id IS NULL AND type=? AND payload=? "
                "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
                (type_, payload_text),
            ).fetchone()
        else:
            old = c.execute(
                "SELECT id FROM tasks WHERE project_id=? AND type=? AND payload=? "
                "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
                (project_id, type_, payload_text),
            ).fetchone()
        if old:
            c.commit()
            return int(old[0])
        cur = c.execute(
            """INSERT INTO tasks(project_id, type, title, payload, status, progress, total, created_at, updated_at)
               VALUES(?,?,?,?, 'pending', 0, ?, ?, ?)""",
            (project_id, type_, title, payload_text, total, now(), now()),
        )
        c.commit()
        return int(cur.lastrowid or 0)


def update_task(task_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {cols} WHERE id=?", tuple(fields.values()) + (task_id,))


def task_row(task_id: int) -> dict | None:
    return q1("SELECT * FROM tasks WHERE id=?", (task_id,))


def claim_pending_task() -> dict | None:
    """跨线程/进程原子领取一条任务，并保证同一项目同一时刻只跑一条任务。"""
    with closing(conn()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            """SELECT t.* FROM tasks t
               WHERE t.status='pending'
                 AND (t.project_id IS NULL OR NOT EXISTS (
                   SELECT 1 FROM tasks active
                   WHERE active.project_id=t.project_id AND active.status='running'
                 ))
               ORDER BY t.id LIMIT 1"""
        ).fetchone()
        if row is None:
            c.commit()
            return None
        c.execute("UPDATE tasks SET status='running', message='开始执行', error='', updated_at=? WHERE id=? AND status='pending'",
                  (now(), row['id']))
        c.commit()
        claimed = dict(row)
        claimed['status'] = 'running'
        claimed['message'] = '开始执行'
        claimed['error'] = ''
        return claimed


def task_status(task_id: int) -> str:
    row = task_row(task_id)
    return row["status"] if row else "missing"
