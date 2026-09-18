# 评审席位抽签服务

基于 FastAPI + SQLite 的可验证加权抽签服务，用于评审席位分配。采用**承诺-揭示（commit-reveal）**机制：创建抽签时服务端生成秘密种子，仅返回其 SHA-256 承诺；执行时结合调用方 nonce 按权重无放回抽样并固化顺序；结束后揭示种子，任何人都可以通过校验接口重放算法，独立验证承诺、nonce 与结果是否一致。

## 快速开始

```bash
docker compose up --build
```

服务启动后访问：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

SQLite 数据保存在名为 `lottery-data` 的卷中（容器内路径 `/data/lottery.db`）。

## 抽签流程

```
创建 POST /lotteries            → 服务端生成种子，只返回 commitment = SHA-256(seed)
执行 POST /lotteries/{id}/execute  → 提交 nonce，按权重无放回抽样，固化中签顺序（仅一次）
揭示 POST /lotteries/{id}/reveal   → 公开种子 seed
校验 POST /lotteries/{id}/verify   → 重放算法，核对承诺 / nonce / 结果
```

### 算法说明

- 每个候选人 `i` 的随机数由 `SHA-256("{seed}|{nonce}|{i}")` 确定性导出，映射到 `(0, 1)`；
- 采用 Efraimidis–Spirakis 加权无放回抽样：`key_i = u_i ** (1 / weight_i)`，key 越大越优先；
- 取 key 最大的前 `seats` 个候选人，降序排列即为固化的中签顺序；
- 算法完全确定：相同的 `(seed, nonce, 候选人列表)` 必然重放出相同结果。

### 示例（curl）

```bash
# 1. 创建抽签：4 名候选人、2 个席位、各自权重
curl -s -X POST http://localhost:8000/lotteries \
  -H 'Content-Type: application/json' \
  -d '{"seats": 2, "candidates": [
        {"name": "alice", "weight": 1},
        {"name": "bob",   "weight": 2},
        {"name": "carol", "weight": 3},
        {"name": "dave",  "weight": 4}]}'
# → {"id": "<ID>", "commitment": "<sha256>", "status": "pending", ...}（不含种子）

# 2. 执行抽签（调用方提供 nonce）
curl -s -X POST http://localhost:8000/lotteries/<ID>/execute \
  -H 'Content-Type: application/json' \
  -d '{"nonce": "meeting-2026-09-18"}'
# → {"result": ["dave", "bob"], "status": "executed", ...}

# 3. 揭示种子
curl -s -X POST http://localhost:8000/lotteries/<ID>/reveal
# → {"seed": "<64 hex>", ...}

# 4. 校验：重放算法并核对承诺、nonce、结果
curl -s -X POST http://localhost:8000/lotteries/<ID>/verify \
  -H 'Content-Type: application/json' \
  -d '{"seed": "<seed>", "nonce": "meeting-2026-09-18"}'
# → {"valid": true, "checks": {"commitment_match": true, ...}, ...}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/lotteries` | 创建抽签，返回 id 与承诺（不返回种子） |
| GET | `/lotteries/{id}` | 查询状态与结果；种子仅在揭示后可见 |
| POST | `/lotteries/{id}/execute` | 提交 nonce 执行抽签，固化结果（仅可执行一次） |
| POST | `/lotteries/{id}/reveal` | 揭示种子（须先执行） |
| POST | `/lotteries/{id}/verify` | 重放校验；揭示后可省略 body 中的 seed/nonce |
| GET | `/healthz` | 健康检查 |

校验响应中 `checks` 分别给出承诺、nonce、结果三项核对结果，`valid` 为三者之与；任一被篡改都会使 `valid` 为 `false`。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                 # 运行测试
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `lottery.db`（容器内 `/data/lottery.db`） | SQLite 数据库文件路径 |
