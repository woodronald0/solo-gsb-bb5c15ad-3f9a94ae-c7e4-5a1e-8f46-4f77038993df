"""FastAPI 入口：评审席位抽签服务。

流程：创建（返回承诺）→ 执行（提交 nonce，固化结果）→ 揭示种子 → 校验重放。
可选分组配额：创建时声明各 group 的 min/max，执行/揭示/校验均重放同一约束算法。
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
    "执行后揭示种子，任何人可重放算法校验结果；支持可选的分组最低/最高名额配额。",
    version="1.1.0",
    lifespan=lifespan,
)


def _get_or_404(lottery_id: str) -> dict[str, Any]:
    record = db.get_lottery(lottery_id)
    if record is None:
        raise HTTPException(status_code=404, detail="抽签记录不存在")
    return record


def _quota_report(record: dict[str, Any]) -> dict[str, Any] | None:
    """已执行记录按存档结果统计分组名额；pending 无结果可统计。"""
    if record["status"] == "pending" or record["result"] is None:
        return None
    return lottery.build_quota_report(
        record["candidates"],
        record["seats"],
        record["result"],
        record["quotas"],
    )


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """对外视图：种子仅在揭示后返回。"""
    data = {
        "id": record["id"],
        "status": record["status"],
        "seats": record["seats"],
        "candidates": record["candidates"],
        "quotas": record["quotas"],
        "commitment": record["commitment"],
        "nonce": record["nonce"],
        "result": record["result"],
        "quota_report": _quota_report(record),
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
        quotas=[q.model_dump() for q in req.quotas] if req.quotas else None,
    )
    return _public(record)


@app.get("/lotteries/{lottery_id}")
def get_lottery(lottery_id: str) -> dict[str, Any]:
    """查询抽签状态与结果（种子仅在揭示后可见）。"""
    return _public(_get_or_404(lottery_id))


@app.post("/lotteries/{lottery_id}/execute")
def execute_lottery(lottery_id: str, req: ExecuteRequest) -> dict[str, Any]:
    """执行抽签：结合 nonce 按权重无放回抽样，固化中签顺序。仅可执行一次。

    声明了分组配额时，抽样须满足各组 min 且不超过 max；配额无法满足时拒绝执行，
    记录保持 pending，调用方可修正 nonce/重试。
    """
    record = _get_or_404(lottery_id)
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="抽签已执行，不能重复执行")
    try:
        result = lottery.draw(
            record["candidates"],
            record["seats"],
            record["seed"],
            req.nonce,
            record["quotas"],
        )
    except lottery.QuotaUnsatisfiableError as exc:
        # 不更新数据库：状态保持 pending，可再次尝试
        raise HTTPException(status_code=422, detail=f"分组配额无法满足：{exc}") from exc
    return _public(db.mark_executed(lottery_id, req.nonce, result))


@app.post("/lotteries/{lottery_id}/reveal")
def reveal_lottery(lottery_id: str) -> dict[str, Any]:
    """抽签结束后揭示种子，供外部校验；同时重放约束算法并报告配额校验。"""
    record = _get_or_404(lottery_id)
    if record["status"] == "pending":
        raise HTTPException(status_code=409, detail="抽签尚未执行，不能揭示种子")
    record = db.mark_revealed(lottery_id)
    data = _public(record)
    # 揭示后可直接用存档 seed/nonce 重放同一约束算法
    replay = _replay(record, record["seed"], record["nonce"])
    data["replay"] = replay
    return data


def _replay(record: dict[str, Any], seed: str, nonce: str) -> dict[str, Any]:
    """用给定 (seed, nonce) 重放（含配额约束的）抽签并核对配额。"""
    try:
        replayed = lottery.draw(
            record["candidates"],
            record["seats"],
            seed,
            nonce,
            record["quotas"],
        )
        error = None
    except lottery.QuotaUnsatisfiableError as exc:
        replayed = []
        error = str(exc)
    return {
        "replayed_result": replayed,
        "result_match": replayed == record["result"] and error is None,
        "quota_report": lottery.build_quota_report(
            record["candidates"],
            record["seats"],
            replayed if error is None else [],
            record["quotas"],
        ),
        "error": error,
    }


@app.post("/lotteries/{lottery_id}/verify")
def verify_lottery(lottery_id: str, req: VerifyRequest) -> dict[str, Any]:
    """重放算法，校验承诺、nonce、结果以及分组配额是否一致。"""
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

    replay_error = None
    try:
        replayed = lottery.draw(
            record["candidates"], record["seats"], seed, nonce, record["quotas"]
        )
    except lottery.QuotaUnsatisfiableError as exc:
        replayed = []
        replay_error = str(exc)

    result_match = replay_error is None and replayed == record["result"]
    # 存档结果本身的配额校验（独立于重放，防止结果被直接篡改）
    stored_quota_report = lottery.build_quota_report(
        record["candidates"], record["seats"], record["result"], record["quotas"]
    )
    replayed_quota_report = lottery.build_quota_report(
        record["candidates"],
        record["seats"],
        replayed if replay_error is None else [],
        record["quotas"],
    )
    quotas_satisfied = (
        stored_quota_report is None or stored_quota_report["satisfied"]
    ) and (replayed_quota_report is None or replayed_quota_report["satisfied"])

    return {
        "id": record["id"],
        "valid": (
            commitment_match
            and nonce_match
            and result_match
            and quotas_satisfied
            and replay_error is None
        ),
        "checks": {
            "commitment_match": commitment_match,
            "nonce_match": nonce_match,
            "result_match": result_match,
            "quotas_satisfied": quotas_satisfied,
        },
        "seed": seed,
        "nonce": nonce,
        "stored_result": record["result"],
        "replayed_result": replayed,
        "stored_quota_report": stored_quota_report,
        "replayed_quota_report": replayed_quota_report,
        "replay_error": replay_error,
    }
