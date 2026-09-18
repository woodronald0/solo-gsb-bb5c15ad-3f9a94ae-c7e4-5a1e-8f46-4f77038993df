"""请求体的 Pydantic 模型与校验规则。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .lottery import QuotaUnsatisfiableError, assert_quotas_feasible


class Candidate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    weight: float = Field(gt=0, description="抽样权重，必须为正数")
    group: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="可选的候选人分组标识；缺省视为未分组",
    )


class GroupQuota(BaseModel):
    group: str = Field(min_length=1, max_length=128, description="分组标识")
    min: int = Field(
        default=0, ge=0, description="该分组必须中签的最低名额（先于权重补足阶段满足）"
    )
    max: int | None = Field(
        default=None,
        ge=0,
        description="该分组中签数上限；缺省表示不限",
    )


class CreateLotteryRequest(BaseModel):
    seats: int = Field(ge=1, description="席位数量")
    candidates: list[Candidate] = Field(min_length=1)
    quotas: list[GroupQuota] | None = Field(
        default=None,
        description="可选的分组配额；缺省或为空表示不启用分组约束（与旧版请求兼容）",
    )

    @model_validator(mode="after")
    def check_consistency(self) -> "CreateLotteryRequest":
        if self.seats > len(self.candidates):
            raise ValueError("席位数不能超过候选人数")
        names = [c.name for c in self.candidates]
        if len(set(names)) != len(names):
            raise ValueError("候选人姓名不能重复")

        if self.quotas:
            quota_names = [q.group for q in self.quotas]
            if len(set(quota_names)) != len(quota_names):
                raise ValueError("配额分组不能重复声明")
            candidate_groups = {c.group for c in self.candidates if c.group is not None}
            unknown = set(quota_names) - candidate_groups
            if unknown:
                raise ValueError(f"配额分组在候选人中不存在：{sorted(unknown)}")
            for q in self.quotas:
                if q.max is not None and q.max < q.min:
                    raise ValueError(f"分组 {q.group!r} 的上限不能小于下限")
            try:
                assert_quotas_feasible(
                    [c.model_dump() for c in self.candidates],
                    self.seats,
                    [q.model_dump() for q in self.quotas],
                )
            except QuotaUnsatisfiableError as exc:
                raise ValueError(str(exc)) from exc
        return self


class ExecuteRequest(BaseModel):
    nonce: str = Field(min_length=1, max_length=256, description="调用方提供的随机 nonce")


class VerifyRequest(BaseModel):
    seed: str | None = Field(default=None, description="已揭示的种子；缺省且已揭示时使用服务端存档")
    nonce: str | None = Field(default=None, description="执行时使用的 nonce；缺省使用服务端存档")
