"""校验 domain/contract.json 与 examples/ 下样例数据的一致性。

检查项：
* 假设必填字段、状态枚举、候选分值范围（原契约样例）；
* catalog.json 中候选边端点必须指向已登记残片的真实断面；
* 组合假设引用的候选必须存在；
* 证据 target 必须指向已声明的候选；
* 分值仅为线索，范围严格落在契约 score_range 内。
"""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
required = set(contract["required_hypothesis_fields"])
low, high = contract["score_range"]
states = set(contract["hypothesis_states"])

# ---- 原候选样例 -----------------------------------------------------------
hypotheses = json.loads(
    (root / "examples" / "hypotheses.json").read_text(encoding="utf-8"))
assert hypotheses and all(required <= set(item) for item in hypotheses)
assert all(item["state"] in states for item in hypotheses)
assert all(low <= edge["score"] <= high
           for item in hypotheses for edge in item["candidate_edges"])
assert len({item["hypothesis_id"] for item in hypotheses}) == len(hypotheses)

# ---- 完整目录样例 ---------------------------------------------------------
catalog = json.loads((root / "examples" / "catalog.json").read_text(encoding="utf-8"))

frag_features = {
    f["fragment_id"]: {feat["feature_id"] for feat in f["features"]}
    for f in catalog["fragments"]
}
for cand in catalog["candidates"]:
    assert low <= cand["score"] <= high, f"{cand['candidate_id']} 分值越界"
    assert cand["rotation"] in contract["candidate_rotation_steps"]
    for side in (cand["left"], cand["right"]):
        assert side["feature_id"] in frag_features.get(side["fragment_id"], set()), \
            f"候选 {cand['candidate_id']} 端点 {side} 不是已登记断面"

candidate_ids = {c["candidate_id"] for c in catalog["candidates"]}
for hyp in catalog["hypotheses"]:
    assert set(hyp["candidate_ids"]) <= candidate_ids, \
        f"{hyp['hypothesis_id']} 引用了不存在的候选"

for ev in catalog["evidence"]:
    assert ev["stance"] in ("supports", "contradicts")
    if ev["target"]["type"] == "candidate":
        assert ev["target"]["id"] in candidate_ids, \
            f"证据 {ev['evidence_id']} 指向未知候选"

for sig in catalog["signatures"]:
    assert sig["signature"] in contract["required_candidate_signatures"] \
        or sig["signature"] in {"curve_profile", "cross_section"}
    assert sig["candidate_id"] in candidate_ids

# 契约自身枚举一致性
assert set(contract["node_types"]) >= {"fragment", "edge_feature", "candidate",
                                       "hypothesis", "evidence", "review"}
assert set(contract["relation_types"]) >= {"describes", "supports", "contradicts"}

print("假设图契约、候选样例与完整目录格式有效")
