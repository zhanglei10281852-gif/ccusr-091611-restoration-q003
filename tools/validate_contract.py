import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
hypotheses = json.loads((root / "examples" / "hypotheses.json").read_text(encoding="utf-8"))
fragments = json.loads((root / "examples" / "fragments.json").read_text(encoding="utf-8"))
experts = json.loads((root / "examples" / "experts.json").read_text(encoding="utf-8"))

required = set(contract["required_hypothesis_fields"])
low, high = contract["score_range"]
assert hypotheses and all(required <= set(item) for item in hypotheses)
assert all(item["state"] in contract["hypothesis_states"] for item in hypotheses)
assert all(low <= edge["score"] <= high for item in hypotheses for edge in item["candidate_edges"])
assert len({item["hypothesis_id"] for item in hypotheses}) == len(hypotheses)

# 残片样例:尺寸与断面特征齐全,断面端点可解析
frag_ids = {frag["fragment_id"] for frag in fragments}
assert len(frag_ids) == len(fragments)
edge_ids = set()
for frag in fragments:
    assert frag["vessel_id"] and frag["dimensions"], frag["fragment_id"]
    for edge_id, feature in frag["edges"].items():
        assert {"orientation_deg", "curvature", "roughness"} <= set(feature), edge_id
        edge_ids.add(f"{frag['fragment_id']}:{edge_id}")

# 假设引用的残片与断面端点必须存在
for item in hypotheses:
    assert set(item["fragment_ids"]) <= frag_ids, item["hypothesis_id"]
    for edge in item["candidate_edges"]:
        assert edge["left"] in edge_ids and edge["right"] in edge_ids, edge

# 专家角色必须在契约声明的角色集合内,签名策略引用的角色亦然
known_roles = set(contract["expert_roles"])
assert all(set(expert["roles"]) <= known_roles for expert in experts)
assert set(contract["signature_policy"]["required_roles"]) <= known_roles

print("假设图契约与候选样例格式有效")
