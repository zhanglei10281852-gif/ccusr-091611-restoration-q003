"""纯领域逻辑：结构校验、确认闸门、状态迁移、评审计票。

不接触数据库与 HTTP，便于单元测试。
"""
from __future__ import annotations

from .contract import (
    REQUIRED_SIGNATURES,
    REVIEW_DECISIONS,
    ROTATION_STEPS,
    SCORE_HIGH,
    SCORE_LOW,
)
from .errors import ConflictError, PermissionDenied, ValidationError

# 假设状态机：key 为当前状态，value 为允许迁移到的状态
STATE_TRANSITIONS = {
    "draft": {"under_review", "withdrawn"},
    "under_review": {"draft", "confirmed", "withdrawn"},
    "confirmed": {"invalidated"},
    "invalidated": set(),
    "withdrawn": set(),
}

ACTIVE_STATES = {"draft", "under_review", "confirmed"}
OCCUPYING_STATES = {"confirmed"}  # 只有已确认组合排他占用残片


# ---------------------------------------------------------------- 基础校验
def require_fields(payload: dict, fields: tuple[str, ...], where: str) -> None:
    missing = [f for f in fields if payload.get(f) in (None, "", [])]
    if missing:
        raise ValidationError(f"{where} 缺少必填字段: {', '.join(missing)}")


def validate_fragment(payload: dict) -> None:
    require_fields(
        payload, ("fragment_id", "group_id", "dimensions", "features"), "残片登记"
    )
    if not isinstance(payload["features"], list) or not payload["features"]:
        raise ValidationError("残片至少要有一个断面特征")
    seen: set[str] = set()
    for feat in payload["features"]:
        require_fields(feat, ("feature_id", "kind"), "断面特征")
        if feat["feature_id"] in seen:
            raise ValidationError(f"断面特征 {feat['feature_id']} 在同一残片上重复")
        seen.add(feat["feature_id"])
    dims = payload["dimensions"]
    for key in ("length_mm", "width_mm", "thickness_mm"):
        if key in dims and (not isinstance(dims[key], (int, float)) or dims[key] <= 0):
            raise ValidationError(f"尺寸 {key} 必须为正数")


def validate_candidate(payload: dict, fragments: dict) -> None:
    require_fields(
        payload,
        ("candidate_id", "group_id", "left", "right", "score", "rotation"),
        "候选邻接",
    )
    score = payload["score"]
    if not isinstance(score, (int, float)) or not (SCORE_LOW <= score <= SCORE_HIGH):
        raise ValidationError(
            f"匹配分值 {score!r} 超出契约范围 [{SCORE_LOW}, {SCORE_HIGH}] —— "
            "分值只表示线索强度，不能替代确认"
        )
    if payload["rotation"] not in ROTATION_STEPS:
        raise ValidationError(
            f"旋转角 {payload['rotation']} 不在允许步长 {sorted(ROTATION_STEPS)} 内"
        )
    left, right = payload["left"], payload["right"]
    for side in (left, right):
        require_fields(side, ("fragment_id", "feature_id"), "候选邻接端点")
    if left["fragment_id"] == right["fragment_id"]:
        raise ValidationError("候选邻接不能连接同一残片的两个断面")
    for side in (left, right):
        feat = find_feature(fragments, side["fragment_id"], side["feature_id"])
        if feat is None:
            raise ValidationError(
                f"端点 {side['fragment_id']}:{side['feature_id']} 不存在，"
                "候选邻接必须挂在已登记残片的真实断面上"
            )
        frag_group = fragments[side["fragment_id"]].get("group_id")
        if frag_group is not None and frag_group != payload["group_id"]:
            raise ValidationError(
                f"候选 {payload['candidate_id']} 跨器物组：残片 "
                f"{side['fragment_id']} 属于 {frag_group}，"
                f"候选声明组为 {payload['group_id']}"
            )


def find_feature(fragments: dict, fragment_id: str, feature_id: str):
    frag = fragments.get(fragment_id)
    if not frag:
        return None
    return frag["features"].get(feature_id)


def validate_evidence(payload: dict) -> None:
    require_fields(
        payload,
        ("evidence_id", "group_id", "kind", "summary", "recorded_by", "target", "stance"),
        "证据",
    )
    if payload["stance"] not in ("supports", "contradicts"):
        raise ValidationError("证据立场 stance 必须是 supports 或 contradicts")
    target = payload["target"]
    require_fields(target, ("type", "id"), "证据指向")
    if target["type"] not in ("candidate", "hypothesis"):
        raise ValidationError("证据只能指向 candidate 或 hypothesis")


# ---------------------------------------------------------------- 确认闸门
def _structure_check(hyp: dict, candidates: dict, fragments: dict) -> list[dict]:
    violations: list[dict] = []
    declared = list(hyp["fragment_ids"])
    used_features: set[tuple[str, str]] = set()
    edge_fragments: set[str] = set()
    for cid in hyp["candidate_ids"]:
        cand = candidates.get(cid)
        if not cand:
            violations.append(
                {"gate": "structure", "candidate_id": cid,
                 "message": f"候选邻接 {cid} 不存在"}
            )
            continue
        if cand["group_id"] != hyp["group_id"]:
            violations.append(
                {"gate": "structure", "candidate_id": cid,
                 "message": f"候选 {cid} 属于其他器物组 {cand['group_id']}"}
            )
        for side in (cand["left"], cand["right"]):
            key = (side["fragment_id"], side["feature_id"])
            if key in used_features:
                violations.append(
                    {"gate": "structure", "path": [f"{side['fragment_id']}:{side['feature_id']}"],
                     "message": "同一断面在组合中被两条邻接重复占用"}
                )
            used_features.add(key)
            edge_fragments.add(side["fragment_id"])
            if side["fragment_id"] not in declared:
                violations.append(
                    {"gate": "structure", "path": [side["fragment_id"]],
                     "message": f"邻接 {cid} 使用了未申报残片 {side['fragment_id']}"}
                )
            if find_feature(fragments, side["fragment_id"], side["feature_id"]) is None:
                violations.append(
                    {"gate": "structure",
                     "path": [f"{side['fragment_id']}:{side['feature_id']}"],
                     "message": "断面对象不存在"}
                )
    extra = [f for f in declared if f not in edge_fragments]
    if extra:
        violations.append(
            {"gate": "structure", "path": extra,
             "message": "申报残片未出现在任何邻接中，无法构成可检查的组合"}
        )
    return violations


def _occupancy_check(hyp: dict, hypotheses: dict) -> list[dict]:
    """残片排他占用：任一残片已被 *其他* 已确认组合占用即否决。"""
    violations = []
    for fid in hyp["fragment_ids"]:
        owners = [
            {"hypothesis_id": other_id, "state": other["state"]}
            for other_id, other in hypotheses.items()
            if other_id != hyp["hypothesis_id"]
            and other["state"] in OCCUPYING_STATES
            and fid in other["fragment_ids"]
        ]
        if owners:
            violations.append({
                "gate": "exclusive_occupancy",
                "fragment_id": fid,
                "path": [hyp["hypothesis_id"], fid,
                         *[f"occupied_by:{o['hypothesis_id']}({o['state']})" for o in owners]],
                "occupied_by": owners,
                "message": f"残片 {fid} 已被确认组合 {[o['hypothesis_id'] for o in owners]} 排他占用",
            })
    return violations


def _orientation_check(hyp: dict, candidates: dict) -> list[dict]:
    """方向一致性：把每条候选边视为两残片间的相对旋转约束，

    BFS 赋予每个残片全局方向；同一残片经由不同路径推出矛盾方向即否决。
    """
    adjacency: dict[str, list[tuple[str, int, str]]] = {}
    for cid in hyp["candidate_ids"]:
        cand = candidates.get(cid)
        if not cand:
            continue
        a, b = cand["left"]["fragment_id"], cand["right"]["fragment_id"]
        adjacency.setdefault(a, []).append((b, cand["rotation"] % 360, cid))
        adjacency.setdefault(b, []).append((a, (-cand["rotation"]) % 360, cid))

    orientation: dict[str, int] = {}
    violations: list[dict] = []
    for start in adjacency:
        if start in orientation:
            continue
        orientation[start] = 0
        stack = [start]
        while stack:
            node = stack.pop()
            for nbr, delta, cid in adjacency[node]:
                implied = (orientation[node] + delta) % 360
                if nbr not in orientation:
                    orientation[nbr] = implied
                    stack.append(nbr)
                elif orientation[nbr] != implied:
                    violations.append({
                        "gate": "orientation",
                        "candidate_id": cid,
                        "path": [start, node, nbr],
                        "message": (
                            f"方向约束冲突：{nbr} 经不同路径推得 {orientation[nbr]}° 与 {implied}°"
                        ),
                    })
    return violations


def _signature_check(hyp: dict, candidates: dict) -> list[dict]:
    """必需签名：每条候选边都必须观测到契约要求的全部三维签名。"""
    violations = []
    for cid in hyp["candidate_ids"]:
        cand = candidates.get(cid)
        if not cand:
            continue
        observed = set(cand.get("signatures", {}))
        missing = [s for s in REQUIRED_SIGNATURES if s not in observed]
        if missing:
            violations.append({
                "gate": "required_signatures",
                "candidate_id": cid,
                "missing": missing,
                "path": [cid],
                "message": f"候选 {cid} 缺少必需签名: {', '.join(missing)}",
            })
    return violations


def _evidence_check(hyp: dict, candidates: dict, evidence: dict) -> list[dict]:
    """对立证据：候选边或假设本身存在未撤回的 contradicts 证据时不得确认。"""
    violations = []
    for cid in hyp["candidate_ids"]:
        against = [
            eid for eid, ev in evidence.items()
            if ev["target"]["type"] == "candidate"
            and ev["target"]["id"] == cid
            and ev["stance"] == "contradicts"
        ]
        if against:
            violations.append({
                "gate": "evidence",
                "candidate_id": cid,
                "evidence_ids": against,
                "path": [cid, *against],
                "message": f"候选 {cid} 存在对立证据 {against}，高分值不能覆盖证据",
            })
    against_hyp = [
        eid for eid, ev in evidence.items()
        if ev["target"]["type"] == "hypothesis"
        and ev["target"]["id"] == hyp["hypothesis_id"]
        and ev["stance"] == "contradicts"
    ]
    if against_hyp:
        violations.append({
            "gate": "evidence",
            "hypothesis_id": hyp["hypothesis_id"],
            "evidence_ids": against_hyp,
            "path": [hyp["hypothesis_id"], *against_hyp],
            "message": f"假设本身存在对立证据 {against_hyp}",
        })
    return violations


def evaluate_gates(hyp: dict, *, fragments: dict, candidates: dict,
                   hypotheses: dict, evidence: dict) -> dict:
    """对一个假设执行确认前全部检查，返回结构化闸门报告（含冲突路径）。"""
    checks_def = [
        ("structure", lambda: _structure_check(hyp, candidates, fragments)),
        ("exclusive_occupancy", lambda: _occupancy_check(hyp, hypotheses)),
        ("orientation", lambda: _orientation_check(hyp, candidates)),
        ("required_signatures", lambda: _signature_check(hyp, candidates)),
        ("evidence", lambda: _evidence_check(hyp, candidates, evidence)),
    ]
    checks = []
    for name, fn in checks_def:
        violations = fn()
        checks.append({"gate": name, "passed": not violations, "violations": violations})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def blocking_violations(report: dict) -> list[dict]:
    return [v for c in report["checks"] for v in c["violations"]]


# ---------------------------------------------------------------- 状态迁移
def ensure_transition(current: str, target: str) -> None:
    if target not in STATE_TRANSITIONS.get(current, set()):
        raise ConflictError(
            f"假设状态不能从 {current} 迁移到 {target}",
            details={"current_state": current, "requested_state": target},
        )


# ---------------------------------------------------------------- 评审计票
def tally_reviews(reviews_for_version: list[dict], experts: dict) -> dict:
    """同一专家在同一版本上的多次意见以最后一条为准（历史意见全部保留），

    离线重传由幂等键去重，计票永远按专家一人一票。
    """
    latest: dict[str, dict] = {}
    for review in sorted(reviews_for_version, key=lambda r: r["seq"]):
        latest[review["expert_id"]] = review
    counts = {"approve": 0, "request_changes": 0, "reject": 0}
    approvers: list[str] = []
    for expert_id, review in latest.items():
        decision = review["decision"]
        if decision not in REVIEW_DECISIONS:
            continue
        counts[decision] += 1
        if decision == "approve":
            approvers.append(expert_id)
    lead_approvers = [
        eid for eid in approvers
        if experts.get(eid, {}).get("role") == "lead_restorer"
    ]
    quorum = (
        counts["reject"] == 0
        and counts["approve"] >= 2
        and bool(lead_approvers)
    )
    return {
        "votes": counts,
        "approvers": approvers,
        "lead_approvers": lead_approvers,
        "quorum": quorum,
        "per_expert_latest": {eid: r["decision"] for eid, r in latest.items()},
    }


def assert_can_confirm(role: str, permissions: dict) -> None:
    if not permissions.get(role, {}).get("can_confirm"):
        raise PermissionDenied(
            f"角色 {role} 无权确认组合；确认需 lead_restorer 执行",
            details={"role": role},
        )


def assert_can_invalidate(role: str, permissions: dict) -> None:
    if not permissions.get(role, {}).get("can_invalidate"):
        raise PermissionDenied(
            f"角色 {role} 无权宣告组合失效", details={"role": role}
        )
