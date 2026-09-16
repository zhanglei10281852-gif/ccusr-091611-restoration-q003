# 陶片拼接假设协作图

项目资料描述残片、断面特征、候选邻接与组合假设之间的关系。候选分值只代表线索强度，专家决策通过独立事件保存，已经形成的历史组合不会因后续调整而丢失。

`domain/contract.json` 列出图节点、关系和决策状态，`examples/hypotheses.json` 展示互斥候选。运行 `python tools/validate_contract.py` 可检查引用与分值范围。

