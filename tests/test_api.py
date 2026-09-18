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
    assert ok["checks"]["commitment_match"] is True
    assert ok["checks"]["nonce_match"] is True
    assert ok["checks"]["result_match"] is True
    assert ok["checks"]["allocation_match"] is True
    assert ok["checks"]["quota_satisfied"] is True
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
    bad_nonce = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "forged"}
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


# ---------------------------------------------------------------------------
# 分组配额
# ---------------------------------------------------------------------------

GROUPED_CANDIDATES = [
    {"name": "a1", "weight": 1, "group": "A"},
    {"name": "a2", "weight": 1, "group": "A"},
    {"name": "a3", "weight": 1, "group": "A"},
    {"name": "b1", "weight": 1, "group": "B"},
    {"name": "b2", "weight": 1, "group": "B"},
    {"name": "free1", "weight": 1},
    {"name": "free2", "weight": 1},
]


def _create_quotas(client, seats, quotas, candidates=None):
    body = {"seats": seats, "candidates": candidates or GROUPED_CANDIDATES, "quotas": quotas}
    resp = client.post("/lotteries", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_quota_min_is_forced_and_caps_respected(client):
    # B 组必须中签 2 人且最多 2 人；A 组最多 1 人；剩余席位落入自由池
    created = _create_quotas(
        client,
        seats=4,
        quotas=[{"group": "A", "min": 0, "max": 1}, {"group": "B", "min": 2, "max": 2}],
    )
    lid = created["id"]

    executed = client.post(f"/lotteries/{lid}/execute", json={"nonce": "n-1"})
    assert executed.status_code == 200, executed.text
    body = executed.json()
    result = body["result"]
    assert len(result) == 4
    group_of = {c["name"]: c.get("group") for c in GROUPED_CANDIDATES}
    counts: dict = {}
    for name in result:
        g = group_of[name]
        counts[g] = counts.get(g, 0) + 1
    assert counts["B"] == 2
    assert counts.get("A", 0) <= 1

    checks = body["quota_checks"]
    assert checks["quotas_enabled"] is True
    assert checks["feasible"] is True
    assert checks["constraints_satisfied"] is True
    by_group = {g["group"]: g for g in checks["groups"]}
    assert by_group["B"]["selected"] == 2
    assert by_group["B"]["within_quota"] is True
    assert by_group["A"]["max"] == 1
    assert by_group["A"]["selected"] <= 1
    # B 占 2、A 至多 1，4 个席位中至少 1 个来自自由池
    assert by_group[None]["selected"] >= 1
    assert by_group[None]["group"] is None  # 自由池以 null 分组呈现


def test_quota_replay_is_deterministic_across_seeds(client):
    from app.lottery import draw_detail

    quotas = [{"group": "A", "min": 1, "max": 2}, {"group": "B", "min": 1}]
    for nonce in [f"nonce-{i}" for i in range(50)]:
        detail = draw_detail(GROUPED_CANDIDATES, 4, "fixed-seed", nonce, quotas)
        result, alloc = detail["result"], detail["allocation"]
        assert len(result) == 4
        assert 1 <= alloc.get("A", 0) <= 2
        assert alloc.get("B", 0) >= 1
        # 同一 (seed, nonce) 必须逐位重放
        again = draw_detail(GROUPED_CANDIDATES, 4, "fixed-seed", nonce, quotas)
        assert again["result"] == result
        assert again["allocation"] == alloc


def test_infeasible_quota_rejected_and_stays_pending(client):
    # 最低名额之和（A=3, B=2）= 5 > 席位 4
    created = _create_quotas(
        client,
        seats=4,
        quotas=[{"group": "A", "min": 3}, {"group": "B", "min": 2}],
    )
    lid = created["id"]

    resp = client.post(f"/lotteries/{lid}/execute", json={"nonce": "n"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "reasons" in detail
    assert any("最低名额" in r for r in detail["reasons"])

    # 状态保持 pending，结果未固化，仍可再次尝试
    fetched = client.get(f"/lotteries/{lid}").json()
    assert fetched["status"] == "pending"
    assert fetched["result"] is None

    # 上限合计装不下席位：A 上限 1、B 上限 1、自由池 2 人 → 最多 4，
    # 改成 seats=5 时候选总数虽够但容量不足
    created2 = _create_quotas(
        client,
        seats=5,
        quotas=[{"group": "A", "max": 1}, {"group": "B", "max": 1}],
    )
    resp2 = client.post(f"/lotteries/{created2['id']}/execute", json={"nonce": "n"})
    assert resp2.status_code == 422
    assert any("上限" in r for r in resp2.json()["detail"]["reasons"])
    assert client.get(f"/lotteries/{created2['id']}").json()["status"] == "pending"


def test_quota_min_exceeds_candidates_rejected(client):
    created = _create_quotas(
        client,
        seats=3,
        quotas=[{"group": "B", "min": 3}],  # B 组只有 2 名候选人
    )
    resp = client.post(f"/lotteries/{created['id']}/execute", json={"nonce": "n"})
    assert resp.status_code == 422
    assert any("候选人数" in r for r in resp.json()["detail"]["reasons"])


def test_quota_create_validation(client):
    # min > max
    resp = client.post(
        "/lotteries",
        json={
            "seats": 2,
            "candidates": GROUPED_CANDIDATES,
            "quotas": [{"group": "A", "min": 2, "max": 1}],
        },
    )
    assert resp.status_code == 422

    # 配额分组不存在
    resp = client.post(
        "/lotteries",
        json={
            "seats": 2,
            "candidates": GROUPED_CANDIDATES,
            "quotas": [{"group": "Z", "min": 1}],
        },
    )
    assert resp.status_code == 422

    # 配额分组重复
    resp = client.post(
        "/lotteries",
        json={
            "seats": 2,
            "candidates": GROUPED_CANDIDATES,
            "quotas": [{"group": "A", "max": 1}, {"group": "A", "max": 2}],
        },
    )
    assert resp.status_code == 422


def test_quota_verify_reports_checks(client):
    created = _create_quotas(
        client,
        seats=4,
        quotas=[{"group": "A", "min": 1, "max": 2}, {"group": "B", "min": 1}],
    )
    lid = created["id"]
    executed = client.post(f"/lotteries/{lid}/execute", json={"nonce": "q-nonce"}).json()
    result = executed["result"]
    seed = client.post(f"/lotteries/{lid}/reveal").json()["seed"]

    ok = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "q-nonce"}
    ).json()
    assert ok["valid"] is True
    assert ok["checks"] == {
        "commitment_match": True,
        "nonce_match": True,
        "result_match": True,
        "allocation_match": True,
        "quota_satisfied": True,
    }
    assert ok["replayed_result"] == result
    assert ok["quota_checks"]["constraints_satisfied"] is True

    # 篡改 nonce：结果重放不一致，配额校验同时失败
    bad = client.post(f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "x"}).json()
    assert bad["valid"] is False
    assert bad["checks"]["result_match"] is False


def test_legacy_ungrouped_request_still_compatible(client):
    # 旧格式请求：候选人无 group，无 quotas
    created = _create(client, seats=2)
    assert created["quotas"] is None
    assert created["quota_checks"] is None
    lid = created["id"]

    executed = client.post(f"/lotteries/{lid}/execute", json={"nonce": "legacy"}).json()
    assert len(executed["result"]) == 2
    assert executed["quota_checks"]["quotas_enabled"] is False
    assert executed["quota_checks"]["feasible"] is True
    assert executed["quota_checks"]["constraints_satisfied"] is True

    seed = client.post(f"/lotteries/{lid}/reveal").json()["seed"]
    ok = client.post(
        f"/lotteries/{lid}/verify", json={"seed": seed, "nonce": "legacy"}
    ).json()
    assert ok["valid"] is True
    assert ok["checks"]["quota_satisfied"] is True


def test_db_migrates_legacy_schema(tmp_path, monkeypatch):
    """旧版本数据库（无 quotas/allocation 列）启动时自动补列并可正常工作。"""
    import sqlite3

    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE lotteries (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, seats INTEGER NOT NULL,
                candidates TEXT NOT NULL, seed TEXT NOT NULL, commitment TEXT NOT NULL,
                nonce TEXT, result TEXT, status TEXT NOT NULL
            )
            """
        )

    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    from app import db as db_module

    db_module.init_db()
    with sqlite3.connect(db_file) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(lotteries)")}
    assert {"quotas", "allocation"} <= cols

    record = db_module.create_lottery(
        seats=1,
        candidates=[{"name": "a", "weight": 1.0}],
        seed="s",
        commitment="c",
    )
    assert record["quotas"] is None
    assert record["allocation"] is None
