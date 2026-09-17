# 陶片拼接假设协作图

多人协作的拼接假设项目:把残片、断面特征、候选邻接、组合假设和专家意见组织成可回溯的关系图。候选分值只代表线索强度,专家决策通过独立事件保存,已经形成的历史组合不会因后续调整而丢失。

## 领域契约与样例

`domain/contract.json` 列出图节点、关系、决策状态、分值范围、专家角色与签名策略;`examples/` 提供残片尺寸与三维断面特征(`fragments.json`)、互斥候选假设(`hypotheses.json`)和专家角色(`experts.json`)。运行 `python tools/validate_contract.py` 可检查引用、角色与分值范围。

## 结构

- `assembly/model.py` — 残片、断面特征、候选邻接、假设、证据、评审等核心模型
- `assembly/guards.py` — 确认前的三项守卫:残片排他占用、方向一致性(带旋转势差的并查集)、必需签名
- `assembly/service.py` — 关系图与追加式事件日志,假设生命周期、评审、证据、确认与查询接口
- `tools/assembly_cli.py` — 命令行接口与演示场景
- `tests/test_assembly.py` — 行为测试(`python -m unittest discover -s tests`)

## 关键规则

- **分值不是定论**:确认只经过守卫与专家签名,算法分值不参与判定;0.3 分签名齐全可确认,0.99 分没有签名也会被驳回。
- **确认守卫**:残片不得被另一已确认组合占用;同一假设内候选边的方向约束必须自洽(含断面边不得重复使用);当前版本须集齐必需角色与最少签名数,有效否决一票拦截。
- **失效不删除**:contradicts 类证据使已确认组合失效并释放残片占用,旧结论与全部事件保留在图中。
- **版本冲突**:调整与确认都携带期望版本号,并发修改抛 `VersionConflictError` 并记入决策史;调整递增版本后,旧版本签名不再计入。
- **离线重传**:评审以提交方生成的 `review_id` 幂等去重,重传只记一条"重复忽略"事件,不重复计票。

## 接口

```bash
python tools/assembly_cli.py demo                 # 演示完整协作过程并保存状态
python tools/assembly_cli.py feasible             # 当前可行组合(已确认/可确认/被拦截及原因)
python tools/assembly_cli.py conflicts            # 残片争夺形成的冲突路径
python tools/assembly_cli.py replay vessel-A      # 重放器物从候选提出到拆解重组的全部决策
```

Python API 入口为 `assembly.AssemblyService`:`propose_hypothesis` / `add_review` / `add_evidence` / `confirm` / `adjust_hypothesis`,查询为 `current_feasible` / `conflict_paths` / `replay`,状态可经 `save` / `load` 持久化。
