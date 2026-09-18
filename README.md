# 评审席位抽签服务

基于 FastAPI + SQLite 的可验证加权抽签服务，用于评审席位分配。采用**承诺-揭示（commit-reveal）**机制：创建抽签时服务端生成秘密种子，仅返回其 SHA-256 承诺；执行时结合调用方 nonce 按权重无放回抽样并固化顺序；结束后揭示种子，任何人都可以通过校验接口重放算法，独立验证承诺、nonce、结果与分组配额是否一致。

## 快速开始

```bash
docker compose up --build
```

服务启动后访问：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

SQLite 数据保存在名为 `lottery-data` 的卷中（容器内路径 `/data/lottery.db`）。容器内置健康检查，`docker compose ps` 与编排依赖可直接使用。

## 抽签流程

```
创建 POST /lotteries               → 服务端生成种子，只返回 commitment = SHA-256(seed)
执行 POST /lotteries/{id}/execute  → 提交 nonce，按权重（在分组配额约束下）无放回抽样，固化中签顺序（仅一次）
揭示 POST /lotteries/{id}/reveal   → 公开种子 seed
校验 POST /lotteries/{id}/verify   → 重放同一约束算法，核对承诺 / nonce / 结果 / 配额
```

### 算法说明

- 每个候选人 `i` 的随机数由 `SHA-256("{seed}|{nonce}|{i}")` 确定性导出，映射到 `(0, 1)`；
- 采用 Efraimidis–Spirakis 加权无放回抽样：`key_i = u_i ** (1 / weight_i)`，key 越大越优先；
- **无分组配额时**：取 key 最大的前 `seats` 个候选人，降序排列即为固化的中签顺序；
- **有分组配额时**（同一条全局 key 序列，完全确定）：
  1. **保底阶段**：每个配额组在组内按 key 降序先取满 `min` 个最低名额；
  2. **补足阶段**：未中签的候选人按 key 全局降序依次补位，已达到该组 `max` 的候选人跳过，直至取满 `seats` 个；
  3. 最终名单仍按 key 全局降序输出；
- 不属于任何配额组的候选人（`group` 缺省）构成不受限的自由池，可填充剩余席位；
- 算法完全确定：相同的 `(seed, nonce, 候选人列表, 配额)` 必然重放出相同结果与分组占用。

### 分组配额的可行性规则

执行时（而非创建时，承诺已固定）检查以下条件，任一不满足则**拒绝执行并保持 `pending`**，返回 `422` 与具体原因，状态、承诺均不变，可稍后重试：

- 某组 `min` 大于该组候选人数；
- 各组 `min` 之和大于总席位数；
- 各组 `max` 上限（未声明 `max` 视为无限制，叠加自由池容量）合计装不下总席位数。

创建时的静态校验：`min <= max`、配额分组名称不重复、每个配额组至少有一名候选人。

### 示例（curl）

#### 不使用分组配额（旧格式，完全兼容）

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
# → {"result": ["dave", "carol"], "status": "executed", ...}

# 3. 揭示种子
curl -s -X POST http://localhost:8000/lotteries/<ID>/reveal
# → {"seed": "<64 hex>", ...}

# 4. 校验：重放算法并核对承诺、nonce、结果
curl -s -X POST http://localhost:8000/lotteries/<ID>/verify \
  -H 'Content-Type: application/json' \
  -d '{"seed": "<seed>", "nonce": "meeting-2026-09-18"}'
# → {"valid": true, "checks": {"commitment_match": true, ...}, ...}
```

#### 使用分组配额

```bash
# A 组（机构甲）与 B 组（机构乙）各有保底与上限；未声明 group 的候选人进入自由池
curl -s -X POST http://localhost:8000/lotteries \
  -H 'Content-Type: application/json' \
  -d '{
    "seats": 5,
    "candidates": [
      {"name": "a1", "weight": 1, "group": "A"},
      {"name": "a2", "weight": 2, "group": "A"},
      {"name": "a3", "weight": 1, "group": "A"},
      {"name": "b1", "weight": 2, "group": "B"},
      {"name": "b2", "weight": 1, "group": "B"},
      {"name": "free1", "weight": 1},
      {"name": "free2", "weight": 2}
    ],
    "quotas": [
      {"group": "A", "min": 2, "max": 3},
      {"group": "B", "min": 1, "max": 2}
    ]
  }'

# 执行；若配额装不下席位会返回 422 且保持 pending
curl -s -X POST http://localhost:8000/lotteries/<ID>/execute \
  -H 'Content-Type: application/json' -d '{"nonce": "meeting-2026-09-18"}'
# → {
#     "result": ["...", ...],
#     "quota_checks": {
#       "quotas_enabled": true,
#       "feasible": true,
#       "constraints_satisfied": true,
#       "reasons": [],
#       "groups": [
#         {"group": "A", "min": 2, "max": 3, "selected": 2, "within_quota": true},
#         {"group": "B", "min": 1, "max": 2, "selected": 1, "within_quota": true},
#         {"group": null, "min": 0, "max": null, "selected": 2, "within_quota": true}
#       ]
#     }, ...
#   }

# 不可行示例响应（HTTP 422，记录保持 pending）：
# {"detail": {"message": "分组配额无法满足，抽签保持 pending",
#             "reasons": ["各组最低名额之和 6 超过席位数 5"]}}

# 校验同样重放配额算法，给出配额核对
curl -s -X POST http://localhost:8000/lotteries/<ID>/verify \
  -H 'Content-Type: application/json' \
  -d '{"seed": "<seed>", "nonce": "meeting-2026-09-18"}'
# → {"valid": true,
#    "checks": {"commitment_match": true, "nonce_match": true,
#               "result_match": true, "allocation_match": true,
#               "quota_satisfied": true},
#    "quota_checks": {...同执行响应...},
#    "stored_allocation": {"A": 2, "B": 1, "null": 2},
#    "replayed_allocation": {...}, ...}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/lotteries` | 创建抽签，返回 id 与承诺（不返回种子）；可携带候选人 `group` 与 `quotas` |
| GET | `/lotteries/{id}` | 查询状态与结果；种子仅在揭示后可见，结果附带 `quota_checks` |
| POST | `/lotteries/{id}/execute` | 提交 nonce 执行抽签，固化结果（仅可执行一次）；配额不可行时 422 并保持 pending |
| POST | `/lotteries/{id}/reveal` | 揭示种子（须先执行） |
| POST | `/lotteries/{id}/verify` | 重放同一约束算法；揭示后可省略 body 中的 seed/nonce |
| GET | `/healthz` | 健康检查 |

### 请求字段

- `candidates[].group`：可选字符串。缺省（或 `null`）表示该候选人不属于任何配额组，进入自由池。
- `quotas[]`：可选，缺省即旧的无分组行为。
  - `group`：组名，必须在候选人中出现；
  - `min`：最低中签名额，默认 `0`；
  - `max`：最高中签名额，可省略或为 `null` 表示不设上限；必须满足 `min <= max`。

校验响应中 `checks` 分别给出承诺、nonce、结果、分组占用与配额满足情况的核对结果，`valid` 为各项之与；任一被篡改都会使 `valid` 为 `false`。`quota_checks.groups` 逐组列出配置与实际占用（自由池以 `group: null` 表示）。

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

旧版本数据库在服务启动时自动迁移（补齐 `quotas` / `allocation` 列）；无分组的历史记录与旧格式请求继续按原算法工作。
