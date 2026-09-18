"""FastAPI 入口：评审席位抽签服务。

流程：创建（返回承诺）→ 执行（提交 nonce，固化结果）→ 揭示种子 → 校验重放。
可选分组配额：创建时声明候选人 group 与各组 min/max，执行、揭示与校验
都重放同一条确定性配额抽样算法。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from . import db, lottery
from .lottery import QuotaInfeasibleError
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


def _quota_checks(record: dict[str, Any]) -> dict[str, Any] | None:
    """根据存档结果生成配额校验报告；pending（尚无结果）时为 None。"""
    if record["result"] is None:
        return None
    allocation = record.get("allocation")
    # 旧记录可能没有 allocation 存档，按结果名反推分组
    return lottery.build_quota_report(
        record["candidates"],
        record["seats"],
        record.get("quotas"),
        record["result"],
        allocation,
    )


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """对外视图：种子仅在揭示后返回。"""
    data = {
        "id": record["id"],
        "status": record["status"],
        "seats": record["seats"],
        "candidates": record["candidates"],
        "quotas": record.get("quotas"),
        "commitment": record["commitment"],
        "nonce": record["nonce"],
        "result": record["result"],
        "quota_checks": _quota_checks(record),
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
    quotas = [q.model_dump() for q in req.quotas] if req.quotas else None
    record = db.create_lottery(
        seats=req.seats,
        candidates=[c.model_dump() for c in req.candidates],
        seed=seed,
        commitment=lottery.commitment_for_seed(seed),
        quotas=quotas,
    )
    return _public(record)


@app.get("/lotteries/{lottery_id}")
def get_lottery(lottery_id: str) -> dict[str, Any]:
    """查询抽签状态与结果（种子仅在揭示后可见）。"""
    return _public(_get_or_404(lottery_id))


@app.post("/lotteries/{lottery_id}/execute")
def execute_lottery(lottery_id: str, req: ExecuteRequest) -> dict[str, Any]:
    """执行抽签：结合 nonce 在配额约束下按权重无放回抽样，固化中签顺序。

    仅可执行一次；配额无法满足时拒绝执行，抽签保持 pending，可更换
    配置（新建抽签）或稍后重试——状态与承诺均不改变。
    """
    record = _get_or_404(lottery_id)
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="抽签已执行，不能重复执行")
    try:
        drawn = lottery.draw_detail(
            record["candidates"],
            record["seats"],
            record["seed"],
            req.nonce,
            record.get("quotas"),
        )
    except QuotaInfeasibleError as exc:
        # 不修改任何状态：记录保持 pending
        raise HTTPException(
            status_code=422,
            detail={"message": "分组配额无法满足，抽签保持 pending", "reasons": exc.reasons},
        )
    return _public(
        db.mark_executed(lottery_id, req.nonce, drawn["result"], drawn["allocation"])
    )


@app.post("/lotteries/{lottery_id}/reveal")
def reveal_lottery(lottery_id: str) -> dict[str, Any]:
    """抽签结束后揭示种子，供外部校验。"""
    record = _get_or_404(lottery_id)
    if record["status"] == "pending":
        raise HTTPException(status_code=409, detail="抽签尚未执行，不能揭示种子")
    return _public(db.mark_revealed(lottery_id))


@app.post("/lotteries/{lottery_id}/verify")
def verify_lottery(lottery_id: str, req: VerifyRequest) -> dict[str, Any]:
    """重放同一约束算法，校验承诺、nonce、结果与分组配额是否一致。"""
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

    replayed: list[str] = []
    replay_allocation: dict[str, int] = {}
    replay_failed = False
    try:
        detail = lottery.draw_detail(
            record["candidates"],
            record["seats"],
            seed,
            nonce,
            record.get("quotas"),
        )
        replayed = detail["result"]
        replay_allocation = detail["allocation"]
    except QuotaInfeasibleError:
        # 篡改 seed/nonce 不会改变可行性；走到这里说明存档配置本身异常
        replay_failed = True

    result_match = not replay_failed and replayed == record["result"]
    stored_allocation = record.get("allocation")
    if stored_allocation is None:
        # 旧记录未存档分组占用，按重放结果对账
        stored_allocation = replay_allocation
    allocation_match = not replay_failed and replay_allocation == stored_allocation
    quota_checks = lottery.build_quota_report(
        record["candidates"],
        record["seats"],
        record.get("quotas"),
        record["result"],
        record.get("allocation"),
    )
    quota_ok = quota_checks["constraints_satisfied"] and result_match

    return {
        "id": record["id"],
        "valid": commitment_match and nonce_match and result_match and quota_ok,
        "checks": {
            "commitment_match": commitment_match,
            "nonce_match": nonce_match,
            "result_match": result_match,
            "allocation_match": allocation_match,
            "quota_satisfied": quota_checks["constraints_satisfied"],
        },
        "quota_checks": quota_checks,
        "seed": seed,
        "nonce": nonce,
        "stored_result": record["result"],
        "replayed_result": replayed,
        "stored_allocation": record.get("allocation"),
        "replayed_allocation": replay_allocation,
    }
