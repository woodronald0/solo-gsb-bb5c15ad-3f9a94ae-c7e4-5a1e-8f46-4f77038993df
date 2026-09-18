"""FastAPI 入口：评审席位抽签服务。

流程：创建（返回承诺）→ 执行（提交 nonce，固化结果）→ 揭示种子 → 校验重放。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from . import db, lottery
from .schemas import CreateLotteryRequest, ExecuteRequest, VerifyRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="评审席位抽签服务",
    description="基于承诺-揭示机制的可验证加权抽签：创建时仅公开种子承诺，"
    "执行后揭示种子，任何人可重放算法校验结果。",
    version="1.0.0",
    lifespan=lifespan,
)


def _get_or_404(lottery_id: str) -> dict[str, Any]:
    record = db.get_lottery(lottery_id)
    if record is None:
        raise HTTPException(status_code=404, detail="抽签记录不存在")
    return record


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """对外视图：种子仅在揭示后返回。"""
    data = {
        "id": record["id"],
        "status": record["status"],
        "seats": record["seats"],
        "candidates": record["candidates"],
        "commitment": record["commitment"],
        "nonce": record["nonce"],
        "result": record["result"],
        "created_at": record["created_at"],
    }
    if record["status"] == "revealed":
        data["seed"] = record["seed"]
    return data


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/lotteries", status_code=201)
def create_lottery(req: CreateLotteryRequest) -> dict[str, Any]:
    """创建抽签：生成秘密种子，仅返回其 SHA-256 承诺。"""
    seed = lottery.generate_seed()
    record = db.create_lottery(
        seats=req.seats,
        candidates=[c.model_dump() for c in req.candidates],
        seed=seed,
        commitment=lottery.commitment_for_seed(seed),
    )
    return _public(record)


@app.get("/lotteries/{lottery_id}")
def get_lottery(lottery_id: str) -> dict[str, Any]:
    """查询抽签状态与结果（种子仅在揭示后可见）。"""
    return _public(_get_or_404(lottery_id))


@app.post("/lotteries/{lottery_id}/execute")
def execute_lottery(lottery_id: str, req: ExecuteRequest) -> dict[str, Any]:
    """执行抽签：结合 nonce 按权重无放回抽样，固化中签顺序。仅可执行一次。"""
    record = _get_or_404(lottery_id)
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="抽签已执行，不能重复执行")
    result = lottery.draw(record["candidates"], record["seats"], record["seed"], req.nonce)
    return _public(db.mark_executed(lottery_id, req.nonce, result))


@app.post("/lotteries/{lottery_id}/reveal")
def reveal_lottery(lottery_id: str) -> dict[str, Any]:
    """抽签结束后揭示种子，供外部校验。"""
    record = _get_or_404(lottery_id)
    if record["status"] == "pending":
        raise HTTPException(status_code=409, detail="抽签尚未执行，不能揭示种子")
    return _public(db.mark_revealed(lottery_id))


@app.post("/lotteries/{lottery_id}/verify")
def verify_lottery(lottery_id: str, req: VerifyRequest) -> dict[str, Any]:
    """重放算法，校验承诺、nonce 与结果是否一致。"""
    record = _get_or_404(lottery_id)
    if record["status"] == "pending":
        raise HTTPException(status_code=409, detail="抽签尚未执行，无结果可校验")

    seed = req.seed
    if seed is None:
        if record["status"] != "revealed":
            raise HTTPException(status_code=400, detail="种子尚未揭示，请在请求体中提供 seed")
        seed = record["seed"]
    nonce = req.nonce if req.nonce is not None else record["nonce"]

    commitment_match = lottery.commitment_for_seed(seed) == record["commitment"]
    nonce_match = nonce == record["nonce"]
    replayed = lottery.draw(record["candidates"], record["seats"], seed, nonce)
    result_match = replayed == record["result"]

    return {
        "id": record["id"],
        "valid": commitment_match and nonce_match and result_match,
        "checks": {
            "commitment_match": commitment_match,
            "nonce_match": nonce_match,
            "result_match": result_match,
        },
        "seed": seed,
        "nonce": nonce,
        "stored_result": record["result"],
        "replayed_result": replayed,
    }
