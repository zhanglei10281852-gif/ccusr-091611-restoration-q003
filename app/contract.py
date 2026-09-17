"""读取 domain/contract.json，集中管理契约常量与角色权限。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "domain" / "contract.json"


def load_contract(path: str | Path | None = None) -> dict:
    text = (Path(path) if path else CONTRACT_PATH).read_text(encoding="utf-8")
    return json.loads(text)


CONTRACT = load_contract()

NODE_TYPES = set(CONTRACT["node_types"])
RELATION_TYPES = set(CONTRACT["relation_types"])
HYPOTHESIS_STATES = set(CONTRACT["hypothesis_states"])
SCORE_LOW, SCORE_HIGH = CONTRACT["score_range"]
REQUIRED_SIGNATURES = tuple(CONTRACT["required_candidate_signatures"])
ROTATION_STEPS = set(CONTRACT["candidate_rotation_steps"])
REVIEW_DECISIONS = set(CONTRACT["review_decisions"])
EXPERT_ROLES = set(CONTRACT["expert_roles"])
ROLE_PERMS = CONTRACT["roles"]
EVENT_TYPES = set(CONTRACT["event_types"])

#: 确认组合时允许使用的候选边状态
ALLOWED_EDGE_STATES = {"proposed", "recommended"}
