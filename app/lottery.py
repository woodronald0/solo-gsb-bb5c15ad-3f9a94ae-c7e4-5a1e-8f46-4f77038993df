"""抽签核心算法：种子承诺与确定性的加权无放回抽样（支持分组配额）。

采用承诺-揭示（commit-reveal）机制：
- 创建时服务端生成秘密种子，仅公开其 SHA-256 承诺；
- 执行时结合调用方 nonce，用 Efraimidis-Spirakis 算法按权重无放回抽样；
- 声明了分组配额（每组 min/max）时，先保证各组最低名额，再按权重补足
  剩余席位且不超过上限；
- 揭示后任何人可用 (seed, nonce) 重放本模块算法，独立验证结果与配额。
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Sequence


class QuotaInfeasibleError(ValueError):
    """分组配额无法满足（如最低名额之和超过席位数）。"""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def generate_seed() -> str:
    """生成 256 位随机种子（64 个十六进制字符）。"""
    return secrets.token_hex(32)


def commitment_for_seed(seed: str) -> str:
    """计算种子的 SHA-256 承诺。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _uniform(seed: str, nonce: str, index: int) -> float:
    """由 (seed, nonce, 候选人序号) 确定性导出 (0, 1) 内的均匀随机数。"""
    digest = hashlib.sha256(f"{seed}|{nonce}|{index}".encode("utf-8")).digest()
    x = int.from_bytes(digest[:8], "big")
    # +1 保证结果落在 (0, 1)，避免取对数/开方时出现 0
    return (x + 1) / (2**64 + 1)


def _normalize_quotas(quotas: Sequence[Any] | None) -> dict[str, dict[str, int]]:
    """把配额声明规整为 {group: {"min": m, "max": M}}；缺省 max 即无上限。"""
    normalized: dict[str, dict[str, int]] = {}
    if not quotas:
        return normalized
    for quota in quotas:
        item = dict(quota)
        maximum = item.get("max")
        normalized[item["group"]] = {
            "min": int(item.get("min", 0)),
            "max": int(maximum) if maximum is not None else None,
        }
    return normalized


def _group_sizes(candidates: Sequence[dict]) -> dict[str | None, int]:
    sizes: dict[str | None, int] = {}
    for candidate in candidates:
        group = candidate.get("group")
        sizes[group] = sizes.get(group, 0) + 1
    return sizes


def check_quota_feasibility(
    candidates: Sequence[dict],
    seats: int,
    quotas: Sequence[Any] | None,
) -> dict[str, Any]:
    """检查分组配额在给定席位下是否可满足。

    规则：
    - 任一组最低名额不得超过该组候选人数；
    - 各组最低名额之和不得超过总席位数；
    - 所有配额组的上限之和（未声明上限视为无限制）至少能容纳总席位。
    """
    quota_map = _normalize_quotas(quotas)
    sizes = _group_sizes(candidates)
    total_min = sum(q["min"] for q in quota_map.values())
    total_capacity = 0
    for group, q in quota_map.items():
        available = sizes.get(group, 0)
        total_capacity += available if q["max"] is None else min(q["max"], available)
    ungrouped = sizes.get(None, 0)
    if not quota_map:
        # 无配额声明：候选人都属于不受限的池子
        total_capacity = sum(sizes.values())
    else:
        total_capacity += ungrouped

    reasons: list[str] = []
    for group in sorted(quota_map):
        q = quota_map[group]
        available = sizes.get(group, 0)
        if q["min"] > available:
            reasons.append(
                f"分组 '{group}' 的最低名额 {q['min']} 超过该组候选人数 {available}"
            )
    if total_min > seats:
        reasons.append(f"各组最低名额之和 {total_min} 超过席位数 {seats}")
    if total_capacity < seats:
        reasons.append(
            f"各分组上限可容纳的席位合计 {total_capacity} 少于席位数 {seats}"
        )

    return {"feasible": not reasons, "reasons": reasons}


def draw_detail(
    candidates: Sequence[dict],
    seats: int,
    seed: str,
    nonce: str,
    quotas: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """带配额的确定性加权无放回抽样，返回中签名单与分组占用情况。

    Efraimidis-Spirakis：每个候选人 i 取 u_i ~ U(0,1)，
    计算 key_i = u_i ** (1 / weight_i)，key 越大越优先。

    配额抽样（仍使用同一条全局 key 序列）：
    1. 各组按 key 降序先取满 min 个（最低名额）；
    2. 未被选中的候选人按 key 全局降序依次补位，
       已达该组 max 的候选人跳过，直至取满 seats 个。
    """
    quota_map = _normalize_quotas(quotas)
    feasibility = check_quota_feasibility(candidates, seats, quotas)
    if not feasibility["feasible"]:
        raise QuotaInfeasibleError(feasibility["reasons"])

    keyed: list[tuple[float, int, str, str | None]] = []
    for index, candidate in enumerate(candidates):
        u = _uniform(seed, nonce, index)
        key = u ** (1.0 / float(candidate["weight"]))
        keyed.append((key, index, candidate["name"], candidate.get("group")))
    # key 相同（概率可忽略）时按提交顺序排序，保证结果完全确定
    keyed.sort(key=lambda item: (-item[0], item[1]))

    chosen: list[tuple[float, int, str]] = []
    chosen_names: set[str] = set()
    per_group: dict[str | None, int] = {}

    def take(item: tuple[float, int, str, str | None]) -> None:
        _, _, name, group = item
        chosen.append((item[0], item[1], name))
        chosen_names.add(name)
        per_group[group] = per_group.get(group, 0) + 1

    # 第一阶段：每个配额组按 key 降序取满 min
    for group in sorted(quota_map):
        minimum = quota_map[group]["min"]
        if minimum == 0:
            continue
        picked = 0
        for item in keyed:
            if picked == minimum:
                break
            if item[3] == group:
                take(item)
                picked += 1

    # 第二阶段：全局按 key 降序补位，跳过已满员（达到 max）的分组
    for item in keyed:
        if len(chosen) == seats:
            break
        if item[2] in chosen_names:
            continue
        group = item[3]
        cap = quota_map.get(group, {}).get("max") if group is not None else None
        if cap is not None and per_group.get(group, 0) >= cap:
            continue
        take(item)

    chosen.sort(key=lambda triple: (-triple[0], triple[1]))
    return {
        "result": [name for _, _, name in chosen],
        "allocation": dict(per_group),
        "feasibility": feasibility,
    }


def draw(
    candidates: Sequence[dict],
    seats: int,
    seed: str,
    nonce: str,
    quotas: Sequence[Any] | None = None,
) -> list[str]:
    """按权重无放回抽样，返回固定顺序的中签名单（长度 = seats）。

    无配额时取 key 最大的前 seats 个；声明配额时先满足各组 min，
    再按 key 补足剩余席位且不超过各组 max。
    """
    return draw_detail(candidates, seats, seed, nonce, quotas)["result"]


def build_quota_report(
    candidates: Sequence[dict],
    seats: int,
    quotas: Sequence[Any] | None,
    result: Sequence[str] | None,
    allocation: dict[str | None, int] | None = None,
) -> dict[str, Any]:
    """构造对外的配额校验报告：可行性、各组配置与实际占用。"""
    quota_map = _normalize_quotas(quotas)
    feasibility = check_quota_feasibility(candidates, seats, quotas)

    if allocation is None and result is not None:
        group_by_name = {c["name"]: c.get("group") for c in candidates}
        allocation = {}
        for name in result:
            group = group_by_name.get(name)
            allocation[group] = allocation.get(group, 0) + 1
    allocation = allocation or {}

    groups = set(quota_map) | set(allocation)
    group_reports: list[dict[str, Any]] = []
    constraints_satisfied = True
    for group in sorted(groups, key=lambda g: (g is None, g or "")):
        q = quota_map.get(group)
        count = allocation.get(group, 0)
        report: dict[str, Any] = {
            "group": group,
            "selected": count,
            "min": q["min"] if q else 0,
            "max": q["max"] if q else None,
        }
        within = True
        if q is not None:
            within = count >= q["min"] and (q["max"] is None or count <= q["max"])
        report["within_quota"] = within
        constraints_satisfied = constraints_satisfied and within
        group_reports.append(report)

    return {
        "quotas_enabled": bool(quota_map),
        "feasible": feasibility["feasible"],
        "reasons": feasibility["reasons"],
        "constraints_satisfied": constraints_satisfied,
        "groups": group_reports,
    }
