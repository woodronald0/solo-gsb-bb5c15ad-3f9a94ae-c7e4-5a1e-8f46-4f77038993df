"""端到端测试：完整走通 创建 → 执行 → 揭示 → 校验 流程。"""
import hashlib

import pytest
from fastapi.testclient import TestClient

CANDIDATES = [
    {"name": "alice", "weight": 1},
    {"name": "bob", "weight": 2},
    {"name": "carol", "weight": 3},
    {"name": "dave", "weight": 4},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def _create(client, seats=2):
    resp = client.post("/lotteries", json={"seats": seats, "candidates": CANDIDATES})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_full_commit_reveal_flow(client):
    created = _create(client)
    lid = created["id"]
    assert len(created["commitment"]) == 64
    assert "seed" not in created  # 创建时不泄露种子
    assert created["status"] == "pending"

    # 执行前不能揭示、不能校验
    assert client.post(f"/lotteries/{lid}/reveal").status_code == 409
    assert client.post(f"/lotteries/{lid}/verify", json={}).status_code == 409

    # 执行：按权重无放回抽样，顺序固化
    executed = client.post(f"/lotteries/{lid}/execute", json={"nonce": "meeting-2026-09-18"})
    assert executed.status_code == 200, executed.text
    result = executed.json()["result"]
    assert len(result) == 2
    assert len(set(result)) == 2  # 无放回
    assert set(result) <= {c["name"] for c in CANDIDATES}

    # 不能重复执行
    assert client.post(f"/lotteries/{lid}/execute", json={"nonce": "again"}).status_code == 409

    # 揭示种子，承诺可核对
    revealed = client.post(f"/lotteries/{lid}/reveal").json()
    seed = revealed["seed"]
    assert hashlib.sha256(seed.encode()).hexdigest() == created["commitment"]

    # 校验通过：承诺、nonce、结果三者一致
    ok = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "meeting-2026-09-18"}
    ).json()
    assert ok["valid"] is True
    assert ok["checks"] == {
        "commitment_match": True,
        "nonce_match": True,
        "result_match": True,
        "quotas_satisfied": True,
    }
    assert ok["replayed_result"] == result

    # 揭示后可省略 seed/nonce，直接校验存档
    ok2 = client.post(f"/lotteries/{lid}/verify", json={}).json()
    assert ok2["valid"] is True

    # 篡改种子 → 承诺不符
    bad_seed = client.post(
        f"/lotteries/{lid}/verify", json={"seed": "00" * 32, "nonce": "meeting-2026-09-18"}
    ).json()
    assert bad_seed["valid"] is False
    assert bad_seed["checks"]["commitment_match"] is False

    # 篡改 nonce → nonce 与结果均不符
    # 确定性地挑一个会重放出不同结果的 nonce，避免抽签偶然撞单（4 选 2 共 12 种有序结果）
    from app import lottery as lottery_mod

    forged = next(
        f"forged-{i}"
        for i in range(1000)
        if lottery_mod.draw(
            [dict(c) for c in CANDIDATES], 2, seed, f"forged-{i}"
        )
        != result
    )
    bad_nonce = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": forged}
    ).json()
    assert bad_nonce["valid"] is False
    assert bad_nonce["checks"]["nonce_match"] is False
    assert bad_nonce["checks"]["result_match"] is False


def test_verify_requires_seed_before_reveal(client):
    lid = _create(client)["id"]
    client.post(f"/lotteries/{lid}/execute", json={"nonce": "n"})
    resp = client.post(f"/lotteries/{lid}/verify", json={})
    assert resp.status_code == 400


def test_unknown_lottery_returns_404(client):
    assert client.get("/lotteries/nope").status_code == 404
    assert client.post("/lotteries/nope/execute", json={"nonce": "n"}).status_code == 404
    assert client.post("/lotteries/nope/reveal").status_code == 404
    assert client.post("/lotteries/nope/verify", json={}).status_code == 404


def test_request_validation(client):
    # 席位多于候选人
    resp = client.post("/lotteries", json={"seats": 5, "candidates": CANDIDATES[:2]})
    assert resp.status_code == 422
    # 权重必须为正
    resp = client.post(
        "/lotteries",
        json={"seats": 1, "candidates": [{"name": "a", "weight": 0}]},
    )
    assert resp.status_code == 422
    # 候选人不能重名
    resp = client.post(
        "/lotteries",
        json={
            "seats": 1,
            "candidates": [{"name": "a", "weight": 1}, {"name": "a", "weight": 2}],
        },
    )
    assert resp.status_code == 422
    # nonce 不能为空
    lid = _create(client)["id"]
    assert client.post(f"/lotteries/{lid}/execute", json={"nonce": ""}).status_code == 422


def test_draw_is_deterministic_and_weighted():
    from app.lottery import draw

    candidates = [{"name": n, "weight": w} for n, w in [("a", 1), ("b", 2), ("c", 3)]]
    assert draw(candidates, 2, "seed", "nonce") == draw(candidates, 2, "seed", "nonce")

    # 统计特性：权重 10:1 时，高权重者应在绝大多数抽签中优先中签
    pair = [{"name": "heavy", "weight": 10}, {"name": "light", "weight": 1}]
    wins = sum(
        draw(pair, 1, "seed", f"nonce-{i}")[0] == "heavy" for i in range(1000)
    )
    assert wins > 800  # 期望约 909/1000
