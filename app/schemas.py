"""请求体的 Pydantic 模型与校验规则。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class Candidate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    weight: float = Field(gt=0, description="抽样权重，必须为正数")
    group: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="候选人分组；缺省表示不参与任何配额约束",
    )


class GroupQuota(BaseModel):
    group: str = Field(min_length=1, max_length=128, description="分组名称")
    min: int = Field(default=0, ge=0, description="该组必须保证的最低中签名额")
    max: Optional[int] = Field(
        default=None, ge=0, description="该组中签名额上限；缺省表示不限"
    )

    @model_validator(mode="after")
    def check_bounds(self) -> "GroupQuota":
        if self.max is not None and self.min > self.max:
            raise ValueError(f"分组 '{self.group}' 的 min({self.min}) 不能大于 max({self.max})")
        return self


class CreateLotteryRequest(BaseModel):
    seats: int = Field(ge=1, description="席位数量")
    candidates: list[Candidate] = Field(min_length=1)
    quotas: Optional[list[GroupQuota]] = Field(
        default=None, description="按分组设置的最低/最高名额；缺省即不启用分组配额"
    )

    @model_validator(mode="after")
    def check_consistency(self) -> "CreateLotteryRequest":
        if self.seats > len(self.candidates):
            raise ValueError("席位数不能超过候选人数")
        names = [c.name for c in self.candidates]
        if len(set(names)) != len(names):
            raise ValueError("候选人姓名不能重复")
        if self.quotas:
            quota_groups = [q.group for q in self.quotas]
            if len(set(quota_groups)) != len(quota_groups):
                raise ValueError("配额分组名称不能重复")
            candidate_groups = {c.group for c in self.candidates if c.group is not None}
            for group in quota_groups:
                if group not in candidate_groups:
                    raise ValueError(f"配额分组 '{group}' 没有对应候选人")
        return self


class ExecuteRequest(BaseModel):
    nonce: str = Field(min_length=1, max_length=256, description="调用方提供的随机 nonce")


class VerifyRequest(BaseModel):
    seed: str | None = Field(default=None, description="已揭示的种子；缺省且已揭示时使用服务端存档")
    nonce: str | None = Field(default=None, description="执行时使用的 nonce；缺省使用服务端存档")
