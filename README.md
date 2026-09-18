# 评审席位抽签服务

基于 FastAPI + SQLite 的可验证加权抽签服务，用于评审席位分配。采用**承诺-揭示（commit-reveal）**机制：创建抽签时服务端生成秘密种子，仅返回其 SHA-256 承诺；执行时结合调用方 nonce 按权重无放回抽样并固化顺序；结束后揭示种子，任何人都可以通过校验接口重放算法，独立验证承诺、nonce、结果与分组配额是否一致。

支持可选的**候选人分组配额**：创建时可为候选人声明 `group`，并为每个分组设置最低名额 `min` 与上限 `max`。不声明任何配额时，行为与旧版无分组抽签完全一致（旧请求、旧数据库均可兼容）。

## 快速开始

```bash
docker compose up --build
```

服务启动后访问：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

SQLite 数据保存在名为 `lottery-data` 的卷中（容器内路径 `/data/lottery.db`）。镜像与 Compose 均配置了基于 `/healthz` 的 HEALTHCHECK。

## 抽签流程

```
创建 POST /lotteries              → 服务端生成种子，只返回 commitment = SHA-256(seed)；可同时声明分组配额
执行 POST /lotteries/{id}/execute → 提交 nonce，按权重无放回抽样（先满足各组 min，再按权重补足且不超 max）
揭示 POST /lotteries/{id}/reveal  → 公开种子 seed，并重放约束算法、报告配额校验
校验 POST /lotteries/{id}/verify  → 重放同一约束算法，核对承诺 / nonce / 结果 / 配额
```

### 算法说明

- 每个候选人 `i` 的随机数由 `SHA-256("{seed}|{nonce}|{i}")` 确定性导出，映射到 `(0, 1)`；
- 采用 Efraimidis–Spirakis 加权无放回抽样：`key_i = u_i ** (1 / weight_i)`，key 越大越优先；
- 抽签完全确定：相同的 `(seed, nonce, 候选人列表, 配额)` 必然重放出相同结果，key 相同时按候选人提交序号决胜。

**无配额时**：取 key 最大的前 `seats` 个候选人，降序排列即为中签顺序（与 v1 逐名单一致）。

**有配额时**，分两阶段在同一 `(seed, nonce)` 下进行：

1. **保下限**：按配额声明顺序处理每个有 `min` 的分组，在该组内按 key 降序选取 `min` 人；
2. **补余量**：所有尚未中签的候选人全局按 key 降序排列，依次补足剩余席位；若候选人所属分组已达 `max` 则跳过。

中签名单的顺序即上述选择顺序（阶段一在前、阶段二在后）。任一阶段发现配额结构上无法满足（某组 `min` 超过组内人数、`min` 之和超过席位数、受 `max` 约束的总容量不足席位等），算法抛出配额不可满足错误：

- **创建时**：请求直接被拒绝（`422`），不会生成抽签记录；
- **执行时**（例如绕过创建校验的边缘情况）：返回 `422`，**记录保持 `pending`**，不写入结果，可再次尝试。

### 分组配额示例（curl）

```bash
# 1. 创建抽签：6 名候选人、4 个席位；A 组 1~2 人、B 组恰好 1 人、未分组候选人不设约束
curl -s -X POST http://localhost:8000/lotteries \
  -H 'Content-Type: application/json' \
  -d '{"seats": 4,
       "candidates": [
         {"name": "a1", "weight": 1, "group": "A"},
         {"name": "a2", "weight": 3, "group": "A"},
         {"name": "a3", "weight": 1, "group": "A"},
         {"name": "b1", "weight": 2, "group": "B"},
         {"name": "b2", "weight": 2, "group": "B"},
         {"name": "x1", "weight": 5}],
       "quotas": [
         {"group": "A", "min": 1, "max": 2},
         {"group": "B", "min": 1, "max": 1}]}'
# → {"id": "<ID>", "commitment": "<sha256>", "status": "pending",
#    "quotas": [{"group": "A", "min": 1, "max": 2}, ...], ...}（不含种子）

# 2. 执行抽签（调用方提供 nonce；配额无法满足时返回 422 且保持 pending）
curl -s -X POST http://localhost:8000/lotteries/<ID>/execute \
  -H 'Content-Type: application/json' \
  -d '{"nonce": "meeting-2026-09-18"}'
# → {"result": ["a2", "b2", "x1", "a3"], "status": "executed",
#    "quota_report": {"enabled": true, "satisfied": true,
#      "groups": [
#        {"group": "A", "min": 1, "max": 2, "selected": 2, "min_ok": true, "max_ok": true},
#        {"group": "B", "min": 1, "max": 1, "selected": 1, "min_ok": true, "max_ok": true},
#        {"group": null, "min": 0, "max": null, "selected": 1, "min_ok": true, "max_ok": true}]}}

# 3. 揭示种子：响应中的 replay 字段会用存档 (seed, nonce) 重放同一约束算法
curl -s -X POST http://localhost:8000/lotteries/<ID>/reveal
# → {"seed": "<64 hex>",
#    "replay": {"replayed_result": [...], "result_match": true,
#               "quota_report": {...}, "error": null}}

# 4. 校验：重放约束算法并核对承诺、nonce、结果与配额
curl -s -X POST http://localhost:8000/lotteries/<ID>/verify \
  -H 'Content-Type: application/json' \
  -d '{"seed": "<seed>", "nonce": "meeting-2026-09-18"}'
# → {"valid": true,
#    "checks": {"commitment_match": true, "nonce_match": true,
#               "result_match": true, "quotas_satisfied": true},
#    "stored_quota_report": {...}, "replayed_quota_report": {...}, ...}
```

### 无分组示例（旧版兼容）

创建请求不传 `group` / `quotas` 即可；候选人默认 `group = null`，响应中 `quotas` 与 `quota_report` 均为 `null`，`checks.quotas_satisfied` 恒为 `true`：

```bash
curl -s -X POST http://localhost:8000/lotteries \
  -H 'Content-Type: application/json' \
  -d '{"seats": 2, "candidates": [
        {"name": "alice", "weight": 1},
        {"name": "bob",   "weight": 2},
        {"name": "carol", "weight": 3},
        {"name": "dave",  "weight": 4}]}'
```

## 请求字段

### `POST /lotteries`

| 字段 | 位置 | 说明 |
| --- | --- | --- |
| `seats` | 顶层 | 席位数量，≥ 1 且不超过候选人数 |
| `candidates[].name` | 候选人 | 唯一姓名（全局不可重复） |
| `candidates[].weight` | 候选人 | 抽样权重，必须为正数 |
| `candidates[].group` | 候选人 | **可选**，1–128 字符的分组标识；缺省为未分组（`null`） |
| `quotas[].group` | 配额 | 分组标识，必须在候选人中存在；同一分组只能声明一次 |
| `quotas[].min` | 配额 | 该组最低名额，默认 0 |
| `quotas[].max` | 配额 | 该组名额上限，缺省（`null`）表示不限；不得小于 `min` |

创建时的配额静态校验（不满足返回 `422`）：

- 配额分组必须存在至少一名候选人，且 `min` 不超过该组候选人数；
- 各组 `min` 之和不超过 `seats`；
- 在各组 `max`（未声明上限的分组按其全部人数计）约束下，总可分配容量不少于 `seats`；
- 候选人未声明 `group` 或其分组未出现在 `quotas` 中都是允许的：该分组不设下限/上限，仅在报告中展示实际中签数。

### 配额报告 `quota_report`

执行/查询结果中附带 `quota_report`（无配额时为 `null`）：

- `enabled`：是否启用配额；
- `satisfied`：中签总数等于席位数且每个分组均满足 `min_ok && max_ok`；
- `groups[]`：按配额声明顺序列出 `group / min / max / selected / min_ok / max_ok`；中签者所属但未声明配额的分组（含未分组 `null`）追加在后，`min = 0, max = null`。

### `POST /lotteries/{id}/verify`

`checks` 包含四项：`commitment_match`、`nonce_match`、`result_match`、`quotas_satisfied`，`valid` 为四项之与。其中：

- `stored_quota_report` 针对**存档结果**做配额核对（即使结果被直接篡改也能发现）；
- `replayed_quota_report` 与 `replayed_result` 是用请求提供（或存档）的 `(seed, nonce)` **重放同一约束算法**的结果；
- 重放若因配额不可满足而失败，`replay_error` 给出原因且 `valid = false`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/lotteries` | 创建抽签，返回 id 与承诺（不返回种子）；可声明分组配额 |
| GET | `/lotteries/{id}` | 查询状态与结果（含 `quota_report`）；种子仅在揭示后可见 |
| POST | `/lotteries/{id}/execute` | 提交 nonce 执行抽签，固化结果（仅可执行一次）；配额不可满足时 422 且保持 pending |
| POST | `/lotteries/{id}/reveal` | 揭示种子（须先执行），并重放约束算法 |
| POST | `/lotteries/{id}/verify` | 重放校验；揭示后可省略 body 中的 seed/nonce |
| GET | `/healthz` | 健康检查 |

## 兼容性与数据迁移

- 旧版创建请求（候选人无 `group`、无 `quotas`）无需任何修改；
- 旧版 SQLite 数据库首次启动时会自动通过 `ALTER TABLE` 增加 `quotas` 列（历史记录该列为 `NULL`，抽签/校验逻辑按无配额处理）；
- 随机数派生公式 `SHA-256("{seed}|{nonce}|{i}")` 与排序决胜规则未变，无配额抽签结果与 v1 完全一致。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                 # 运行测试（含配额专项测试 tests/test_quotas.py）
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `lottery.db`（容器内 `/data/lottery.db`） | SQLite 数据库文件路径 |
