"""仅追加（append-only）事件存储，SQLite 实现。

事件不可修改、不可删除：后续新证据只能追加 invalidation 一类的新事件，
旧结论永远保留，可通过 seq 顺序重放完整决策链。

两个唯一性约束支撑并发正确性：
* (aggregate_type, aggregate_id, version) —— 同一假设的两个 version=N
  修订不可能同时落库，第二位修复师会收到版本冲突；
* dedup_key —— 离线意见重传使用客户端生成的幂等键，重复提交不会二次计票。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    version        INTEGER,
    actor          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL,
    dedup_key      TEXT UNIQUE,
    UNIQUE(aggregate_type, aggregate_id, version)
);
CREATE INDEX IF NOT EXISTS idx_events_agg ON events(aggregate_type, aggregate_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    # ---- 写入 -----------------------------------------------------------
    def append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        actor: str,
        payload: dict[str, Any],
        *,
        version: int | None = None,
        event_id: str | None = None,
        dedup_key: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """追加一个事件。

        返回 (event, reused)：dedup_key 命中历史事件时 reused=True，
        调用方可据此识别"离线重传"而非新投票。
        """
        import uuid

        event_id = event_id or str(uuid.uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            if dedup_key is not None:
                row = self.conn.execute(
                    "SELECT * FROM events WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is not None:
                    return self._row_to_dict(row), True
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                cur = self.conn.execute(
                    """INSERT INTO events
                       (event_id, event_type, aggregate_type, aggregate_id,
                        version, actor, created_at, payload, dedup_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, event_type, aggregate_type, aggregate_id,
                     version, actor, created_at, body, dedup_key),
                )
                self.conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self.conn.execute("ROLLBACK")
                msg = str(exc)
                if "events.dedup_key" in msg:
                    # 并发的同键重传抢先落库：取回原事件当作幂等命中
                    row = self.conn.execute(
                        "SELECT * FROM events WHERE dedup_key = ?", (dedup_key,)
                    ).fetchone()
                    if row is not None:
                        return self._row_to_dict(row), True
                    raise
                # 版本竞争或 event_id 冲突：明确报给上层翻译为 409
                raise ConcurrentVersionError(aggregate_id, version) from exc
            row = self.conn.execute(
                "SELECT * FROM events WHERE seq = ?", (cur.lastrowid,)
            ).fetchone()
            return self._row_to_dict(row), False

    # ---- 读取 -----------------------------------------------------------
    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE aggregate_type=? AND aggregate_id=? ORDER BY seq",
            (aggregate_type, aggregate_id),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def all_events(
        self,
        *,
        after_seq: int = 0,
        event_types: Iterable[str] | None = None,
    ) -> list[dict]:
        if event_types:
            marks = ",".join("?" for _ in event_types)
            rows = self.conn.execute(
                f"SELECT * FROM events WHERE seq > ? AND event_type IN ({marks}) ORDER BY seq",
                (after_seq, *event_types),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq", (after_seq,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_event(self, seq: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        return self._row_to_dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


class ConcurrentVersionError(sqlite3.IntegrityError):
    """同一聚合版本号已被其他事务占用（乐观锁冲突）。"""

    def __init__(self, aggregate_id: str, version: int | None):
        super().__init__(f"version conflict on {aggregate_id} at version {version}")
        self.aggregate_id = aggregate_id
        self.version = version
