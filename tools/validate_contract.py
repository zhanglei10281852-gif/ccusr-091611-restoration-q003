import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
hypotheses = json.loads((root / "examples" / "hypotheses.json").read_text(encoding="utf-8"))
required = set(contract["required_hypothesis_fields"])
low, high = contract["score_range"]
assert hypotheses and all(required <= set(item) for item in hypotheses)
assert all(item["state"] in contract["hypothesis_states"] for item in hypotheses)
assert all(low <= edge["score"] <= high for item in hypotheses for edge in item["candidate_edges"])
assert len({item["hypothesis_id"] for item in hypotheses}) == len(hypotheses)
print("假设图契约与候选样例格式有效")

