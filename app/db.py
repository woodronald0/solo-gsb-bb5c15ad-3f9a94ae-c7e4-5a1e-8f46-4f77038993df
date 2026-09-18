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
    candidates  TEXT NOT NULL,          -- JSON: [{"name": ..., "weight": ...}]
    seed        TEXT NOT NULL,          -- 揭示前仅服务端可见
    commitment  TEXT NOT NULL,
    nonce       TEXT,                   -- 执行时由调用方提供
    result      TEXT,                   -- JSON: [name, ...]，固化后的中签顺序
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


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["candidates"] = json.loads(record["candidates"])
    record["result"] = json.loads(record["result"]) if record["result"] else None
    return record


def create_lottery(
    seats: int,
    candidates: list[dict],
    seed: str,
    commitment: str,
) -> dict[str, Any]:
    lottery_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO lotteries (id, created_at, seats, candidates, seed, commitment, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (lottery_id, created_at, seats, json.dumps(candidates), seed, commitment),
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]


def get_lottery(lottery_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM lotteries WHERE id = ?", (lottery_id,)
        ).fetchone()
    return _row_to_dict(row)


def mark_executed(lottery_id: str, nonce: str, result: list[str]) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "UPDATE lotteries SET nonce = ?, result = ?, status = 'executed' WHERE id = ?",
            (nonce, json.dumps(result), lottery_id),
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]


def mark_revealed(lottery_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "UPDATE lotteries SET status = 'revealed' WHERE id = ?", (lottery_id,)
        )
    return get_lottery(lottery_id)  # type: ignore[return-value]
