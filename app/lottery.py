"""抽签核心算法：种子承诺与带分组配额的加权无放回抽样。

采用承诺-揭示（commit-reveal）机制：
- 创建时服务端生成秘密种子，仅公开其 SHA-256 承诺；
- 执行时结合调用方 nonce，用 Efraimidis-Spirakis 算法按权重无放回抽样；
- 揭示后任何人可用 (seed, nonce) 重放本模块算法，独立验证结果。

分组配额（可选）：
- 候选人可声明 group，每个分组可设置 min/max；
- 阶段一：先为每个有下限的分组在组内按权重取够 min；
- 阶段二：剩余席位在全局按权重补足，但任一分组中签数不得超过 max；
- 配额无法满足时抛出 QuotaUnsatisfiableError，调用方应拒绝执行并保持 pending。

未声明任何配额时，抽样结果与无分组的旧算法逐字节一致。
"""
from __future__ import annotations

import hashlib
import secrets
from collections import Counter
from typing import Any, Sequence


class QuotaUnsatisfiableError(ValueError):
    """分组配额在当前候选人与席位下无法满足。"""


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


def _normalize_quotas(
    quotas: Sequence[dict] | None,
) -> dict[str, tuple[int, int | None]]:
    """把配额列表转为 {group: (min, max)}，max=None 表示不限；保留声明顺序。"""
    if not quotas:
        return {}
    return {
        q["group"]: (int(q["min"]), None if q.get("max") is None else int(q["max"]))
        for q in quotas
    }


def assert_quotas_feasible(
    candidates: Sequence[dict], seats: int, quotas: Sequence[dict] | None
) -> None:
    """静态可行性校验：候选人结构与席位确定后，配额是否理论上可满足。

    创建时与执行时共用本检查；执行抽签时还会再次防御性校验。
    """
    bounds = _normalize_quotas(quotas)
    sizes: Counter[Any] = Counter(c.get("group") for c in candidates)

    total_min = 0
    for group, (mn, mx) in bounds.items():
        size = sizes.get(group, 0)
        if size == 0:
            raise QuotaUnsatisfiableError(f"配额分组 {group!r} 没有任何候选人")
        if mn > size:
            raise QuotaUnsatisfiableError(
                f"分组 {group!r} 仅有 {size} 名候选人，无法满足最低名额 {mn}"
            )
        if mx is not None and mx < mn:
            raise QuotaUnsatisfiableError(
                f"分组 {group!r} 的上限 {mx} 小于下限 {mn}"
            )
        total_min += mn

    if total_min > seats:
        raise QuotaUnsatisfiableError(
            f"各组最低名额之和 {total_min} 超过席位总数 {seats}"
        )

    # 上限容量：每个分组最多贡献 min(组内人数, max)，无上限分组贡献全部人数
    capacity = 0
    for group, size in sizes.items():
        _, mx = bounds.get(group, (0, None))
        capacity += size if mx is None else min(size, mx)
    if capacity < seats:
        raise QuotaUnsatisfiableError(
            f"受各组上限约束，最大可分配名额为 {capacity}，不足席位总数 {seats}"
        )


def draw(
    candidates: Sequence[dict],
    seats: int,
    seed: str,
    nonce: str,
    quotas: Sequence[dict] | None = None,
) -> list[str]:
    """按权重无放回抽样，返回固定顺序的中签名单（长度 = seats）。

    Efraimidis-Spirakis：每个候选人 i 取 u_i ~ U(0,1)，
    计算 key_i = u_i ** (1 / weight_i)，key 越大越优先。

    存在分组配额时分两阶段：
    1. 按配额声明顺序，在每个有 min 的分组内取 key 最大的 min 人（保障下限）；
    2. 其余候选人全局按 key 降序依次补足席位，已达 max 的分组跳过（遵守上限）。

    中签顺序即上述选择顺序；key 相同（概率可忽略）时按提交序号排序，
    因此相同的 (seed, nonce, 候选人列表, 配额) 必然重放出相同结果。
    """
    bounds = _normalize_quotas(quotas)
    assert_quotas_feasible(candidates, seats, quotas)

    # (key, 提交序号, 姓名, 分组)
    keyed: list[tuple[float, int, str, Any]] = []
    for index, candidate in enumerate(candidates):
        u = _uniform(seed, nonce, index)
        key = u ** (1.0 / float(candidate["weight"]))
        keyed.append((key, index, candidate["name"], candidate.get("group")))

    by_group: dict[Any, list[tuple[float, int, str, Any]]] = {}
    for item in keyed:
        by_group.setdefault(item[3], []).append(item)
    for items in by_group.values():
        items.sort(key=lambda it: (-it[0], it[1]))

    chosen: list[tuple[float, int, str, Any]] = []
    chosen_indexes: set[int] = set()
    counts: Counter[Any] = Counter()

    # 阶段一：保障各组最低名额（配额按声明顺序处理，组内按权重）
    for group, (mn, _mx) in bounds.items():
        if mn <= 0:
            continue
        pool = by_group.get(group, [])
        if len(pool) < mn:  # 防御性检查：理论上已被 assert_quotas_feasible 拦截
            raise QuotaUnsatisfiableError(
                f"分组 {group!r} 候选人不足，无法满足最低名额 {mn}"
            )
        for item in pool[:mn]:
            chosen_indexes.add(item[1])
            chosen.append(item)
            counts[group] += 1

    if len(chosen) > seats:
        raise QuotaUnsatisfiableError(
            f"各组最低名额之和 {len(chosen)} 超过席位总数 {seats}"
        )

    # 阶段二：全局按权重补足，受各组上限约束
    remaining = [it for it in keyed if it[1] not in chosen_indexes]
    remaining.sort(key=lambda it: (-it[0], it[1]))
    for item in remaining:
        if len(chosen) >= seats:
            break
        group = item[3]
        _mn, mx = bounds.get(group, (0, None))
        if mx is not None and counts[group] >= mx:
            continue
        chosen_indexes.add(item[1])
        chosen.append(item)
        counts[group] += 1

    if len(chosen) < seats:
        raise QuotaUnsatisfiableError("受分组上限约束，无法补足全部席位")

    return [name for _, _, name, _ in chosen]


def build_quota_report(
    candidates: Sequence[dict],
    seats: int,
    result: Sequence[str],
    quotas: Sequence[dict] | None,
) -> dict[str, Any] | None:
    """根据中签结果统计各分组名额并核对 min/max；无配额时返回 None。

    执行结果、揭示与 verify 共用同一报告结构，配额校验口径完全一致。
    """
    if not quotas:
        return None

    bounds = _normalize_quotas(quotas)
    group_of = {c["name"]: c.get("group") for c in candidates}
    selected_counts: Counter[Any] = Counter(
        group_of.get(name) for name in result
    )

    groups: list[dict[str, Any]] = []
    reported: set[Any] = set()
    for quota in quotas:
        group = quota["group"]
        reported.add(group)
        mn, mx = bounds[group]
        selected = selected_counts.get(group, 0)
        groups.append(
            {
                "group": group,
                "min": mn,
                "max": mx,
                "selected": selected,
                "min_ok": selected >= mn,
                "max_ok": mx is None or selected <= mx,
            }
        )

    # 候选人使用了但未声明配额的分组（含未分组 null）：不受约束，仅作展示
    for group, selected in selected_counts.items():
        if group in reported:
            continue
        groups.append(
            {
                "group": group,
                "min": 0,
                "max": None,
                "selected": selected,
                "min_ok": True,
                "max_ok": True,
            }
        )

    satisfied = len(result) == seats and all(
        g["min_ok"] and g["max_ok"] for g in groups
    )
    return {
        "enabled": True,
        "satisfied": satisfied,
        "seats": seats,
        "selected": len(result),
        "groups": groups,
    }
