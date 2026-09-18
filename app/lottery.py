"""抽签核心算法：种子承诺与加权无放回抽样。

采用承诺-揭示（commit-reveal）机制：
- 创建时服务端生成秘密种子，仅公开其 SHA-256 承诺；
- 执行时结合调用方 nonce，用 Efraimidis-Spirakis 算法按权重无放回抽样；
- 揭示后任何人可用 (seed, nonce) 重放本模块算法，独立验证结果。
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Sequence


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


def draw(candidates: Sequence[dict], seats: int, seed: str, nonce: str) -> list[str]:
    """按权重无放回抽样，返回固定顺序的中签名单（长度 = seats）。

    Efraimidis-Spirakis：每个候选人 i 取 u_i ~ U(0,1)，
    计算 key_i = u_i ** (1 / weight_i)，key 越大越优先。
    取 key 最大的前 seats 个，其降序排列即为中签顺序。
    """
    keyed: list[tuple[float, int, str]] = []
    for index, candidate in enumerate(candidates):
        u = _uniform(seed, nonce, index)
        key = u ** (1.0 / float(candidate["weight"]))
        keyed.append((key, index, candidate["name"]))
    # key 相同（概率可忽略）时按提交顺序排序，保证结果完全确定
    keyed.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, _, name in keyed[:seats]]
