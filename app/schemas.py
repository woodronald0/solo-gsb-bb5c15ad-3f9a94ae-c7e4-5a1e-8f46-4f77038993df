"""请求体的 Pydantic 模型与校验规则。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class Candidate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    weight: float = Field(gt=0, description="抽样权重，必须为正数")


class CreateLotteryRequest(BaseModel):
    seats: int = Field(ge=1, description="席位数量")
    candidates: list[Candidate] = Field(min_length=1)

    @model_validator(mode="after")
    def check_consistency(self) -> "CreateLotteryRequest":
        if self.seats > len(self.candidates):
            raise ValueError("席位数不能超过候选人数")
        names = [c.name for c in self.candidates]
        if len(set(names)) != len(names):
            raise ValueError("候选人姓名不能重复")
        return self


class ExecuteRequest(BaseModel):
    nonce: str = Field(min_length=1, max_length=256, description="调用方提供的随机 nonce")


class VerifyRequest(BaseModel):
    seed: str | None = Field(default=None, description="已揭示的种子；缺省且已揭示时使用服务端存档")
    nonce: str | None = Field(default=None, description="执行时使用的 nonce；缺省使用服务端存档")
