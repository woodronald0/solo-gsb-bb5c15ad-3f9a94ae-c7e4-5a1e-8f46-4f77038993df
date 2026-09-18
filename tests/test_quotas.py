"""分组配额功能测试：约束算法、执行拒绝、重放校验、创建时校验与向后兼容。"""
import pytest
from fastapi.testclient import TestClient

CANDIDATES = [
    {"name": "a1", "weight": 1, "group": "A"},
    {"name": "a2", "weight": 1, "group": "A"},
    {"name": "a3", "weight": 1, "group": "A"},
    {"name": "b1", "weight": 1, "group": "B"},
    {"name": "b2", "weight": 1, "group": "B"},
    {"name": "x1", "weight": 1, "group": None},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def _create(client, seats, candidates=CANDIDATES, quotas=None):
    body: dict = {"seats": seats, "candidates": candidates}
    if quotas is not None:
        body["quotas"] = quotas
    resp = client.post("/lotteries", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _execute(client, lid, nonce="nonce-1"):
    return client.post(f"/lotteries/{lid}/execute", json={"nonce": nonce})


def _group_of(name):
    return {c["name"]: c["group"] for c in CANDIDATES}[name]


def test_min_quota_is_respected(client):
    # 3 席、B 组至少 2 人：即使权重相同（抽签），B 也必须占 2 席
    created = _create(client, 3, quotas=[{"group": "B", "min": 2}])
    assert created["quotas"] == [{"group": "B", "min": 2, "max": None}]

    executed = _execute(client, created["id"])
    assert executed.status_code == 200, executed.text
    body = executed.json()
    result = body["result"]
    assert len(result) == 3
    assert sum(_group_of(n) == "B" for n in result) >= 2

    report = body["quota_report"]
    assert report["enabled"] is True
    assert report["satisfied"] is True
    b_report = next(g for g in report["groups"] if g["group"] == "B")
    assert b_report["min"] == 2
    assert b_report["max"] is None
    assert b_report["selected"] >= 2
    assert b_report["min_ok"] is True
    assert b_report["max_ok"] is True
    # 未声明配额的分组（含未分组 null）若中签也出现在报告中且不报错
    assert sum(g["selected"] for g in report["groups"]) == 3
    by_group = {g["group"]: g for g in report["groups"]}
    assert by_group["B"]["selected"] >= 2


def test_max_quota_is_respected_across_nonces(client):
    # 3 席、A 组最多 1 人；遍历多个 nonce，配额必须每次都成立
    for nonce in [f"n-{i}" for i in range(30)]:
        created = _create(client, 3, quotas=[{"group": "A", "max": 1}])
        body = _execute(client, created["id"], nonce=nonce).json()
        result = body["result"]
        assert sum(_group_of(n) == "A" for n in result) <= 1, (nonce, result)
        assert body["quota_report"]["satisfied"] is True


def test_min_and_max_together(client):
    # 4 席：A 恰好 1 席，B 恰好 2 席，其余 1 席只能来自未分组 x1
    created = _create(
        client, 4, quotas=[{"group": "A", "min": 1, "max": 1},
                           {"group": "B", "min": 2, "max": 2}]
    )
    body = _execute(client, created["id"]).json()
    result = body["result"]
    assert len([n for n in result if n.startswith("a")]) == 1
    assert len([n for n in result if n.startswith("b")]) == 2
    assert "x1" in result
    assert len(result) == len(set(result))  # 无放回
    assert body["quota_report"]["satisfied"] is True


def test_unsatisfiable_min_sum_rejected_at_creation(client):
    # min 之和超过席位 → 创建即 422
    resp = client.post(
        "/lotteries",
        json={
            "seats": 2,
            "candidates": CANDIDATES,
            "quotas": [{"group": "A", "min": 2}, {"group": "B", "min": 2}],
        },
    )
    assert resp.status_code == 422, resp.text


def test_unsatisfiable_capacity_rejected_at_creation(client):
    # A 上限 1、B 上限 1，未分组 x1 只有 1 人 → 最多 2 席，创建 4 席被拒
    resp = client.post(
        "/lotteries",
        json={
            "seats": 4,
            "candidates": CANDIDATES,
            "quotas": [{"group": "A", "max": 1}, {"group": "B", "max": 1}],
        },
    )
    assert resp.status_code == 422, resp.text


def test_quota_group_without_candidates_rejected(client):
    resp = client.post(
        "/lotteries",
        json={
            "seats": 1,
            "candidates": CANDIDATES,
            "quotas": [{"group": "C", "min": 1}],
        },
    )
    assert resp.status_code == 422


def test_min_exceeds_group_size_rejected(client):
    resp = client.post(
        "/lotteries",
        json={
            "seats": 5,
            "candidates": CANDIDATES,
            "quotas": [{"group": "B", "min": 3}],  # B 只有 2 人
        },
    )
    assert resp.status_code == 422


def test_max_smaller_than_min_rejected(client):
    resp = client.post(
        "/lotteries",
        json={
            "seats": 3,
            "candidates": CANDIDATES,
            "quotas": [{"group": "A", "min": 2, "max": 1}],
        },
    )
    assert resp.status_code == 422


def test_duplicate_quota_group_rejected(client):
    resp = client.post(
        "/lotteries",
        json={
            "seats": 3,
            "candidates": CANDIDATES,
            "quotas": [{"group": "A", "min": 1}, {"group": "A", "max": 2}],
        },
    )
    assert resp.status_code == 422


def test_execute_failure_keeps_pending_and_is_retriable(client, monkeypatch):
    """绕过创建时校验，模拟执行时配额不可满足：必须 422 且保持 pending。"""
    from app import lottery

    created = _create(client, 2, quotas=[{"group": "B", "min": 1}])
    lid = created["id"]

    original_draw = lottery.draw

    def boom(candidates, seats, seed, nonce, quotas=None):
        raise lottery.QuotaUnsatisfiableError("模拟不可满足")

    monkeypatch.setattr(lottery, "draw", boom)
    resp = _execute(client, lid)
    assert resp.status_code == 422
    assert "配额" in resp.json()["detail"]
    monkeypatch.setattr(lottery, "draw", original_draw)

    # 记录仍是 pending，可再次执行；重复 nonce 也不影响重试
    assert client.get(f"/lotteries/{lid}").json()["status"] == "pending"
    ok = _execute(client, lid)
    assert ok.status_code == 200
    assert client.get(f"/lotteries/{lid}").json()["status"] == "executed"


def test_reveal_and_verify_report_quotas(client):
    created = _create(
        client, 3, quotas=[{"group": "A", "min": 1, "max": 2}]
    )
    lid = created["id"]
    _execute(client, lid)

    revealed = client.post(f"/lotteries/{lid}/reveal").json()
    assert revealed["replay"]["result_match"] is True
    replay_report = revealed["replay"]["quota_report"]
    assert replay_report["satisfied"] is True
    assert 1 <= next(g for g in replay_report["groups"] if g["group"] == "A")["selected"] <= 2

    seed = revealed["seed"]
    verified = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "nonce-1"}
    ).json()
    assert verified["valid"] is True
    assert verified["checks"]["quotas_satisfied"] is True
    assert verified["stored_quota_report"]["satisfied"] is True
    assert verified["replayed_quota_report"]["satisfied"] is True
    assert verified["replay_error"] is None


def test_verify_fails_when_result_tampered(client):
    created = _create(client, 2, quotas=[{"group": "A", "min": 2}])
    lid = created["id"]
    _execute(client, lid)
    client.post(f"/lotteries/{lid}/reveal")  # 揭示后 verify 可省略 seed/nonce

    # 直接篡改存档结果为不含任何 A 组成员 → 配额校验失败
    import json
    import os
    import sqlite3

    db_path = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE lotteries SET result = ? WHERE id = ?",
            (json.dumps(["b1", "b2"]), lid),
        )

    verified = client.post(f"/lotteries/{lid}/verify", json={}).json()
    assert verified["valid"] is False
    assert verified["checks"]["quotas_satisfied"] is False
    stored = verified["stored_quota_report"]
    a_report = next(g for g in stored["groups"] if g["group"] == "A")
    assert a_report["min_ok"] is False


def test_backward_compatible_without_groups(client):
    # 旧格式请求：候选人无 group、无 quotas
    candidates = [
        {"name": "alice", "weight": 1},
        {"name": "bob", "weight": 2},
        {"name": "carol", "weight": 3},
    ]
    created = client.post(
        "/lotteries", json={"seats": 2, "candidates": candidates}
    ).json()
    assert created["quotas"] is None
    assert all("group" in c and c["group"] is None for c in created["candidates"])

    body = _execute(client, created["id"], nonce="legacy").json()
    assert body["quota_report"] is None

    client.post(f"/lotteries/{created['id']}/reveal")
    verified = client.post(
        f"/lotteries/{created['id']}/verify", json={}
    ).json()
    assert verified["valid"] is True
    assert verified["checks"]["quotas_satisfied"] is True
    assert verified["stored_quota_report"] is None
    assert verified["replayed_quota_report"] is None


def test_legacy_candidate_dicts_without_group_key():
    """历史记录的候选人 JSON 没有 group 键，draw/report 必须按未分组处理。"""
    from app import lottery
    from app.lottery import _uniform

    legacy = [{"name": "a", "weight": 1}, {"name": "b", "weight": 2}]
    keyed = []
    for index, c in enumerate(legacy):
        keyed.append((_uniform("s", "n", index) ** (1.0 / c["weight"]), index, c["name"]))
    keyed.sort(key=lambda item: (-item[0], item[1]))
    result = lottery.draw(legacy, 1, "s", "n", None)
    assert result == [keyed[0][2]]
    assert lottery.build_quota_report(legacy, 1, result, None) is None


def test_draw_without_quotas_matches_legacy_algorithm():
    """无配额时新 draw 必须与旧版纯 ES 抽样逐名单一致。"""
    from app import lottery

    candidates = [
        {"name": n, "weight": w, "group": None}
        for n, w in [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    ]
    # 手工复现旧算法
    keyed = []
    for index, c in enumerate(candidates):
        u = lottery._uniform("seed", "nonce", index)
        keyed.append((u ** (1.0 / c["weight"]), index, c["name"]))
    keyed.sort(key=lambda item: (-item[0], item[1]))
    expected = [n for _, _, n in keyed[:2]]

    assert lottery.draw(candidates, 2, "seed", "nonce") == expected
    assert lottery.draw(candidates, 2, "seed", "nonce", None) == expected
    assert lottery.draw(candidates, 2, "seed", "nonce", []) == expected


def test_draw_is_deterministic_with_quotas():
    from app import lottery

    quotas = [{"group": "A", "min": 1, "max": 2}]
    first = lottery.draw(CANDIDATES, 3, "seed", "nonce", quotas)
    second = lottery.draw(CANDIDATES, 3, "seed", "nonce", quotas)
    assert first == second
    assert len(first) == 3


def test_min_phase_uses_within_group_weights():
    """阶段一在组内按权重选人：A 组权重 100 的候选人应稳定占据 min 名额。"""
    from app import lottery

    candidates = [
        {"name": "heavy", "weight": 100, "group": "A"},
        {"name": "light", "weight": 1, "group": "A"},
        {"name": "b1", "weight": 1, "group": "B"},
    ]
    wins = 0
    for i in range(200):
        result = lottery.draw(
            candidates, 2, "seed", f"n-{i}", [{"group": "A", "min": 1}]
        )
        if "heavy" in result:
            wins += 1
    assert wins > 190
