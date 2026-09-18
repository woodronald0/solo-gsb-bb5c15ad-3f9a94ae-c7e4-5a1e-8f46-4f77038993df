"""SQLite 存取层：抽签记录的创建、查询与状态流转。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS lotteries (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    seats       INTEGER NOT NULL,
    candidates  TEXT NOT NULL,          -- JSON: [{"name": ..., "weight": ..., "group": ...}]
    quotas      TEXT,                   -- JSON: [{"group": ..., "min": ..., "max": ...}]，可空
    seed        TEXT NOT NULL,          -- 揭示前仅服务端可见
    commitment  TEXT NOT NULL,
    nonce       TEXT,                   -- 执行时由调用方提供
    result      TEXT,                   -- JSON: [name, ...]，固化后的中签顺序
    allocation  TEXT,                   -- JSON: [{"group": <name|null>, "count": n}, ...]
    status      TEXT NOT NULL           -- pending / executed / revealed
);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "lottery.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 兼容旧库：为早期版本创建的表补齐新增列
        existing = {row[1] for row in conn.execute("PRAGMA table_info(lotteries)")}
        if "quotas" not in existing:
            conn.execute("ALTER TABLE lotteries ADD COLUMN quotas TEXT")
        if "allocation" not in existing:
            conn.execute("ALTER TABLE lotteries ADD COLUMN allocation TEXT")


def _load_json(value: str | None) -> Any:
    return json.loads(value) if value else None


def _dump_allocation(allocation: dict[str | None, int] | None) -> str | None:
    """分组占用序列化为 [{"group": <name|null>, "count": n}, ...]。

    JSON 对象键必须是字符串，无法表达 None 分组，故用成对列表存储。
    """
    if allocation is None:
        return None
    return json.dumps(
        [{"group": group, "count": count} for group, count in allocation.items()]
    )


def _load_allocation(value: str | None) -> dict[str | None, int] | None:
    payload = _load_json(value)
    if payload is None:
        return None
    return {item["group"]: item["count"] for item in payload}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["candidates"] = _load_json(record["candidates"])
    record["quotas"] = _load_json(record["quotas"])
    record["result"] = _load_json(record["result"])
    record["allocation"] = _load_allocation(record["allocation"])
    return record


def quota_dump(quotas: list[dict] | None) -> list[dict] | None:
    """把配额序列化为可 JSON 存储的形式（旧记录无配额列时返回 None）。"""
    if not quotas:
        return None
    return [{"group": q["group"], "min": int(q["min"]), "max": q["max"]} for q in quotas]


def create_lottery(
    seats: int,
    candidates: list[dict],
    seed: str,
    commitment: str,
    quotas: list[dict] | None = None,
) -> dict[str, Any]:
    lottery_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO lotteries (id, created_at, seats, candidates, quotas, seed, commitment, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                lottery_id,
                created_at,
                seats,
                json.dumps(candidates),
                json.dumps(quota_dump(quotas)),
                seed,
                commitment,
            ),
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]


def get_lottery(lottery_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM lotteries WHERE id = ?", (lottery_id,)
        ).fetchone()
    return _row_to_dict(row)


def mark_executed(
    lottery_id: str,
    nonce: str,
    result: list[str],
    allocation: dict[str, int] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE lotteries
            SET nonce = ?, result = ?, allocation = ?, status = 'executed'
            WHERE id = ?
            """,
            (
                nonce,
                json.dumps(result),
                _dump_allocation(allocation),
                lottery_id,
            ),
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]


def mark_revealed(lottery_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "UPDATE lotteries SET status = 'revealed' WHERE id = ?", (lottery_id,)
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]
