# 陶片拼接假设协作图 (ceramic-assembly-hypotheses)

多人协作的陶片拼接假设系统：把**残片、断面特征、候选邻接、组合假设、专家意见、
证据**组织成一张可回溯的关系图。核心原则——

> **算法匹配分值只代表线索强度，永远不直接构成结论。** 任何组合确认前必须依次通过
> 排他占用、方向一致性、必需签名（及对立证据）闸门；后来的新证据可以使已确认组合
> 失效，但旧结论、旧意见作为历史事件永久保留、随时可重放。

仅依赖 Python 3.11 标准库（`sqlite3` / `http.server`），无需安装任何第三方包。

---

## 1. 快速开始

```bash
# 校验契约与样例数据
python3 tools/validate_contract.py

# 运行全部 34 个测试（纯领域闸门 / 服务并发与幂等 / HTTP 接口）
python3 -m unittest discover -s tests

# 端到端决策回放演示：候选提出 → 评分高分被闸门否决 → 评审确认
# → 残片排他冲突 → 并发修订冲突 → 新证据失效 → 拆解重组 → 全量重放
python3 tools/demo_replay.py

# 启动 HTTP 服务并灌入 vessel-guan-07 基线（持久化到 data/graph.db）
python3 -m app --seed --port 8080
```

---

## 2. 为什么是事件溯源 + 关系图

评审会上需要准确回答"**哪些证据改变了结论**"。因此系统不就地修改状态，而是只追加
（append-only）事件，当前状态由事件流确定性重放得到：

```
expert_registered → fragment_registered → candidate_proposed
  → edge_signature_observed → evidence_recorded
  → hypothesis_created(v1) → hypothesis_revised(v2…)
  → hypothesis_submitted → review_cast(…)
  → hypothesis_confirmed
  → hypothesis_invalidated(trigger_evidence=…)     # 旧 confirmed 事件仍在
```

* **不删除旧结论**：`invalidated` 是追加的新事件，`hypothesis_confirmed`、当时的
  评审意见与闸门依据全部留在流中（见 §6 重放）。
* **关系图**由事件物化：节点 `fragment / edge_feature / candidate / hypothesis /
  evidence / review`，关系 `describes / joins / supports / contradicts / contains /
  reviews / supersedes`。`GET /graph` 直接给出全部带 `since_seq`（关系自哪个事件起
  成立）的边，可导入图可视化。

事件表上的两个 UNIQUE 约束是并发正确性的最终防线：

| 约束 | 保证 |
|---|---|
| `(aggregate_type, aggregate_id, version)` | 同一假设的同一版本只能落库一次 → 两位修复师并发修订，后到者**必然**收到 409 |
| `dedup_key` | 离线意见重传幂等 → 同一条意见重放**绝不二次计票** |

---

## 3. 确认前三道闸门（外加证据闸）

`POST /hypotheses/{id}/confirm` 时，服务端对假设的**当前版本**执行结构化检查，
任何一道不过都返回 `422 confirmation_gate_failed`，并给出每条违规的**冲突路径
`path`**，便于在图上定位：

1. **结构 (structure)**：候选边真实存在且属于同器物组；每条候选的两个端点都是已申报
   残片上的真实断面；同一断面不能被组合内两条邻接重复占用；申报残片必须都被邻接覆盖。
2. **残片排他占用 (exclusive_occupancy)**：任一残片已被**另一个 `confirmed`**
   组合占用即否决。失效（`invalidated`）自动释放占用。
3. **方向一致性 (orientation)**：把每条候选边的相对旋转角（允许 0/90/180/270°）当作
   约束，BFS 给每个残片赋全局方向；同一残片经不同路径推出矛盾方向即否决。
4. **必需签名 (required_signatures)**：每条候选边必须观测到契约要求的全部三维签名
   （`curve_profile` 曲线轮廓、`cross_section` 横截面）。
5. **对立证据 (evidence)**：候选边或假设本身存在未撤回的 `contradicts` 证据时不得确认。

此外确认还有**社会闸门**：评审共识 ≥ 2 票赞成、无反对票，且至少一名
`lead_restorer` 赞成；只有首席修复师可以执行确认与失效。`cand-22` 分值 0.77 但缺
横截面签名，即使凑齐全部赞成票仍被第 4 道闸门拦下——分值不能覆盖证据与程序。

完整闸门报告任何时候都可通过 `GET /hypotheses/{id}` 查看（含每道闸的
`passed/violations`），不依赖是否正在确认。

---

## 4. HTTP 接口

动作接口可用请求体里的显式操作者字段，或 `X-Expert-Id` 请求头。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/snapshot` | 全量当前读模型（残片/候选/假设/证据/意见/关系） |
| GET | `/graph` | 仅关系边 `{from, relation, to, since_seq}` |
| GET | `/feasibility?group_id=` | **当前可行组合 + 冲突路径**：每假设逐闸门结果 + 互斥对（共享残片） |
| GET | `/hypotheses/{id}` | 假设详情：当前状态、闸门报告、意见、本版本计票、时间线 |
| GET | `/groups/{gid}/replay` | **重放**某一器物从候选提出到拆解重组的全部决策（按 seq 排序） |
| GET | `/events?after_seq=` | 原始事件流（增量同步用） |
| POST | `/experts` `/fragments` `/evidence` `/candidates` | 登记专家 / 残片 / 证据 / 候选邻接 |
| POST | `/candidates/{id}/signatures` | 补录三维签名观测 |
| POST | `/hypotheses` | 创建假设（v1） |
| POST | `/hypotheses/{id}/revisions` | **乐观锁修订**：必须带 `expected_version`，失配返回 409 |
| POST | `/hypotheses/{id}/submit` | 提交评审（draft → under_review） |
| POST | `/hypotheses/{id}/reviews` | 投专家意见；`client_request_id` 为**离线幂等键** |
| POST | `/hypotheses/{id}/confirm` | 首席修复师确认（跑全部闸门 + 共识） |
| POST | `/hypotheses/{id}/invalidate` | 首席修复师凭新证据宣告失效（可带 `superseded_by`） |
| POST | `/hypotheses/{id}/withdraw` | 撤回草案 |

状态机：`draft → under_review → confirmed → invalidated`（终态）；
`draft/under_review → withdrawn`（终态）。终态不可修订、不可复活——要重组就创建新
假设，并用 `supersedes` 关系指向旧假设。

错误码：`400 validation_error` · `403 permission_denied` · `404 not_found` ·
`409 conflict`（版本冲突 / 非法状态迁移 / 幂等键内容不一致）·
`422 confirmation_gate_failed`（闸门报告在 `details.gate_report`）。

### curl 示例

```bash
# 两份方案当前的可行性与互斥路径（frag-09 被同时需要）
curl -s 'http://127.0.0.1:8080/feasibility?group_id=vessel-guan-07'

# 离线意见：弱网下重试同一请求，服务端按 client_request_id 去重
curl -s -XPOST http://127.0.0.1:8080/hypotheses/hyp-21/reviews \
  -H 'Content-Type: application/json' -d '{
    "review_id":"rev-lin-1","expert_id":"lin","decision":"approve",
    "comment":"断口弧度与截面纹理均吻合",
    "client_request_id":"offline-device-a/lin/hyp-21/001"}'
```

幂等语义细节：同一 `client_request_id` 重传时返回 `replayed: true` 且不新增事件、
不改票数；若重传体的实质内容（假设/专家/结论/评论）与首次不一致，返回 **409**
而不是静默覆盖。

---

## 5. 样例数据：frag-09 的归属之争

`examples/catalog.json` 是基线叙事（`examples/hypotheses.json` 保留原契约最小样例）：

* `frag-01`（口沿）、`frag-09`（肩部）、`frag-14`（上腹）；
* `cand-21`：frag-01↔frag-09，分值 **0.82**，两项必需签名齐全；
* `cand-22`：frag-09↔frag-14，分值 **0.77**，仅有曲线轮廓，**缺横截面签名**；
* `hyp-21` 与 `hyp-22` 两份草稿同时占用 `frag-09`。

`tools/demo_replay.py` 在内存库中把整个故事演完：高分方案被签名闸否决 → hyp-21 经
正式评审确认 → hyp-22 再确认被排他占用闸拦下 → 两位修复师并发修订 hyp-22 产生
409 → 显微薄片证据显示 frag-01 与 frag-09 烧成温度不同 → hyp-21 失效（历史完整
保留）→ 重组为 hyp-23 并确认，frag-09 占用随失效释放。

---

## 6. 决策重放：评审会怎么解释结论变化

```bash
curl -s http://127.0.0.1:8080/groups/vessel-guan-07/replay
```

返回该器物按事件 `seq` 排序的完整时间线，每一步带事件类型、操作者、时间戳与载荷。
例如可以清楚地解释："hyp-21 在 seq 25 因三项签名与评审共识确认；seq 28 出现
`ev-firing-mismatch` 显微证据 contradicts cand-21；seq 30 首席修复师据此宣告失效并
指定 hyp-23 接替（`supersedes`）；hyp-23 在补齐签名与评审后于 seq 37 确认。"
旧的确认事件（seq 25）和三条赞成意见不会被擦除。

---

## 7. 目录结构

```
domain/contract.json        图节点/关系、状态机、签名与角色权限契约
examples/hypotheses.json    原最小候选样例（保留）
examples/catalog.json       vessel-guan-07 完整基线数据
app/contract.py             契约常量与角色权限
app/errors.py               领域错误（携带 HTTP 状态码）
app/store.py                append-only SQLite 事件存储（版本唯一/幂等唯一约束）
app/domain.py               纯领域逻辑：校验、五道闸门、状态机、计票
app/projection.py           事件重放 → 当前读模型 + 关系图物化
app/service.py              命令编排（乐观锁/幂等/闸门/权限）与查询
app/seed.py                 样例数据装载（幂等，重复 --seed 跳过已存在）
app/api.py                  stdlib HTTP JSON 接口
tools/validate_contract.py  契约与样例一致性校验
tools/demo_replay.py        端到端决策回放演示
tests/                      34 个测试：领域闸门 / 服务 / HTTP
```
