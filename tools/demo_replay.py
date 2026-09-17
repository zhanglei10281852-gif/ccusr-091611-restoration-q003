#!/usr/bin/env python3
"""端到端决策回放演示（不依赖网络，内存事件库）。

叙事线：
  1. 两组高分方案争抢 frag-09 —— 给出互斥路径；
  2. 高分支值不是定论：cand-22 缺必需签名，确认被闸门拦下；
  3. 离线意见重传用幂等键去重，绝不重复计票；
  4. hyp-21 获评审共识并通过全部闸门后确认；
  5. hyp-22 再试确认，被残片排他占用闸门拦下；
  6. 两位修复师并发调整 hyp-22，后到者收到版本冲突；
  7. 新显微证据否定 cand-21，首席修复师宣告 hyp-21 失效（旧结论保留）；
  8. 重组为 hyp-23，证据齐备后确认 —— frag-09 排他占用随失效释放；
  9. 重放器物 vessel-guan-07 从候选提出到拆解重组的全部决策。

用法: python tools/demo_replay.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import ConflictError, GateError, PermissionDenied  # noqa: E402
from app.seed import seed_baseline  # noqa: E402
from app.service import AssemblyService  # noqa: E402
from app.store import EventStore  # noqa: E402


class Reporter:
    def __init__(self, as_json: bool = False):
        self.as_json = as_json
        self.log: list[dict] = []

    def step(self, title: str, **detail) -> None:
        self.log.append({"step": title, **detail})
        if not self.as_json:
            print(f"\n=== {title} ===")
            if detail:
                print(json.dumps(detail, ensure_ascii=False, indent=2))

    def expect_blocked(self, title: str, fn) -> None:
        try:
            fn()
        except (GateError, ConflictError, PermissionDenied) as exc:
            self.step(f"[已拦截] {title}", status=exc.status, code=exc.code,
                      message=exc.message, details=exc.details)
        else:
            raise AssertionError(f"{title} 本应被拦截却成功了")


def run() -> dict:
    svc = AssemblyService(EventStore(":memory:"))
    report = Reporter()

    seed_baseline(svc)
    report.step("基线已建立：frag-01 / frag-09 / frag-14，cand-21(0.82) 与 cand-22(0.77) 争抢 frag-09")

    # 1. 可行组合与冲突路径 ------------------------------------------------
    feasibility = svc.feasibility("vessel-guan-07")
    report.step("当前组合可行性与互斥路径",
                mutual_exclusion=feasibility["mutual_exclusion"],
                gates=[{k: c[k] for k in ("hypothesis_id", "state", "gates_passed")}
                       for c in feasibility["combinations"]])

    # 2. cand-22 分值 0.77 但缺横截面签名，分值不能替代确认 ------------------
    svc.submit_for_review("hyp-22", "chen")
    # 即使人为凑齐赞成票（先不投，直接由首席确认），闸门也应拦下
    report.expect_blocked(
        "cand-22 缺必需签名却仅凭 0.77 高分请求确认",
        lambda: _force_review_quorum(svc, "hyp-22") or svc.confirm_hypothesis("hyp-22", "zhao"),
    )

    # 3. hyp-21 走正式评审：离线意见 + 幂等重传 -----------------------------
    svc.submit_for_review("hyp-21", "chen")
    r1 = svc.cast_review({
        "review_id": "rev-lin-1", "hypothesis_id": "hyp-21",
        "expert_id": "lin", "decision": "approve",
        "comment": "断口弧度与截面纹理均吻合",
        "client_request_id": "offline-device-a/lin/hyp-21/001",
    })
    retransmit = svc.cast_review({
        "review_id": "rev-lin-1", "hypothesis_id": "hyp-21",
        "expert_id": "lin", "decision": "approve",
        "comment": "断口弧度与截面纹理均吻合",
        "client_request_id": "offline-device-a/lin/hyp-21/001",
    })
    report.step("林分析师离线意见重传：识别为重放，票数不重复累加",
                first_vote_votes=r1["tally"]["votes"],
                replayed=retransmit["replayed"],
                votes_after_retransmit=retransmit["tally"]["votes"])
    svc.cast_review({
        "review_id": "rev-chen-1", "hypothesis_id": "hyp-21",
        "expert_id": "chen", "decision": "approve",
        "comment": "断面证据支持",
        "client_request_id": "phone-chen/hyp-21/001",
    })
    svc.cast_review({
        "review_id": "rev-zhao-1", "hypothesis_id": "hyp-21",
        "expert_id": "zhao", "decision": "approve",
        "comment": "同意确认，证据链完整",
        "client_request_id": "desk-zhao/hyp-21/001",
    })

    # 非首席角色无权确认
    report.expect_blocked("陈修复师尝试越权确认 hyp-21",
                          lambda: svc.confirm_hypothesis("hyp-21", "chen"))

    confirmed = svc.confirm_hypothesis("hyp-21", "zhao")
    report.step("首席修复师确认 hyp-21：闸门全部通过",
                gates=[{k: c[k] for k in ("gate", "passed")}
                       for c in confirmed["gate_report"]["checks"]])

    # 4. hyp-22 再确认：残片排他占用 ---------------------------------------
    report.expect_blocked(
        "hyp-22 请求确认：frag-09 已被 hyp-21 排他占用",
        lambda: svc.confirm_hypothesis("hyp-22", "zhao"),
    )

    # 5. 两位修复师并发调整 hyp-22 -----------------------------------------
    first = svc.revise_hypothesis({
        "hypothesis_id": "hyp-22", "expected_version": 1,
        "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
        "revised_by": "chen", "revision_note": "补充备注，等待横截面复测",
    })
    report.expect_blocked(
        "赵师傅基于过期 v1 并发修订 hyp-22：暴露版本冲突",
        lambda: svc.revise_hypothesis({
            "hypothesis_id": "hyp-22", "expected_version": 1,
            "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
            "revised_by": "zhao", "revision_note": "我也在同时调整",
        }),
    )
    report.step("陈修复师修订成功，hyp-22 升至 v2", version=first["version"])

    # 6. 新证据出现 → hyp-21 失效（历史结论不删除） -------------------------
    svc.propose_candidate({
        "candidate_id": "cand-23", "group_id": "vessel-guan-07",
        "left": {"fragment_id": "frag-09", "feature_id": "e4"},
        "right": {"fragment_id": "frag-14", "feature_id": "e3"},
        "score": 0.71, "rotation": 90, "proposed_by": "lin",
    })
    svc.record_evidence({
        "evidence_id": "ev-firing-mismatch", "group_id": "vessel-guan-07",
        "kind": "thin_section_microscopy",
        "summary": "显微薄片显示 frag-01 烧成温度明显高于 frag-09，"
                   "二者不可能属于同一器体；cand-21 的弧度吻合为次生相似",
        "recorded_by": "lin",
        "target": {"type": "candidate", "id": "cand-21"},
        "stance": "contradicts",
    })
    svc.create_hypothesis({
        "hypothesis_id": "hyp-23", "group_id": "vessel-guan-07",
        "fragment_ids": ["frag-09", "frag-14"],
        "candidate_ids": ["cand-23"], "created_by": "zhao",
        "note": "显微证据重组方案：frag-09 与 frag-14",
    })
    invalidated = svc.invalidate_hypothesis({
        "hypothesis_id": "hyp-21", "invalidated_by": "zhao",
        "reason": "烧成温度显微证据否定 cand-21，弧度相似不构成同器依据",
        "evidence_id": "ev-firing-mismatch", "superseded_by": "hyp-23",
    })
    detail_history = svc.hypothesis_detail("hyp-21")
    report.step(
        "新证据使 hyp-21 失效；确认事实与全部意见仍在历史中可查",
        invalidated=invalidated,
        retained_state=detail_history["hypothesis"]["state"],
        retained_confirmed_by=detail_history["hypothesis"].get("confirmed_by"),
        retained_reviews=len(detail_history["reviews"]),
        trigger_evidence=detail_history["hypothesis"].get("trigger_evidence"),
    )

    # 7. hyp-23 证据齐备并确认：占用随失效释放 ------------------------------
    # 补登记 cand-23 的两项必需签名（挂在显微复测证据下）
    for sig, value in [
        ("curve_profile", {"arc_radius_mm": 68.7, "tolerance_mm": 1.0}),
        ("cross_section", {"wedge_angle_deg": 30.8, "layer_count": 3}),
    ]:
        svc.observe_signature({
            "candidate_id": "cand-23", "signature": sig, "value": value,
            "observed_by": "lin",
        })
    svc.submit_for_review("hyp-23", "zhao")
    for rid, expert, decision, cid, comment in [
        ("rev-lin-23", "lin", "approve", "scope-lin/hyp-23/001", "同烧成温度且签名吻合"),
        ("rev-chen-23", "chen", "approve", "phone-chen/hyp-23/001", "同意重组"),
        ("rev-zhao-23", "zhao", "approve", "desk-zhao/hyp-23/001", "确认新组合"),
    ]:
        svc.cast_review({
            "review_id": rid, "hypothesis_id": "hyp-23", "expert_id": expert,
            "decision": decision, "comment": comment, "client_request_id": cid,
        })
    confirm23 = svc.confirm_hypothesis("hyp-23", "zhao")
    report.step("hyp-23 通过全部闸门并确认（frag-09 占用已随 hyp-21 失效释放）",
                gates=[{k: c[k] for k in ("gate", "passed")}
                       for c in confirm23["gate_report"]["checks"]])

    # 8. 全量决策重放 ------------------------------------------------------
    timeline = svc.replay_group("vessel-guan-07")
    compact = [f"{s['seq']:>2} {s['event_type']:<28} {s['actor']:<5} {_gist(s)}"
               for s in timeline["timeline"]]
    report.step(f"器物 vessel-guan-07 决策重放（共 {len(compact)} 个事件）",
                timeline=compact)
    return {"steps": report.log, "timeline": timeline}


def _force_review_quorum(svc: AssemblyService, hyp_id: str) -> None:
    """凑齐评审共识，用以证明"共识之外闸门照样独立生效"。"""
    for rid, expert, cid in [
        (f"rev-lin-{hyp_id}", "lin", f"scope-lin/{hyp_id}/001"),
        (f"rev-chen-{hyp_id}", "chen", f"phone-chen/{hyp_id}/001"),
        (f"rev-zhao-{hyp_id}", "zhao", f"desk-zhao/{hyp_id}/001"),
    ]:
        svc.cast_review({
            "review_id": rid, "hypothesis_id": hyp_id, "expert_id": expert,
            "decision": "approve", "comment": "测试：分值之外的闸门",
            "client_request_id": cid,
        })


def _gist(step: dict) -> str:
    p = step["payload"]
    if step["event_type"] == "candidate_proposed":
        return f"{p['candidate_id']} score={p['score']} {p['left']['fragment_id']}~{p['right']['fragment_id']}"
    if step["event_type"] in ("hypothesis_created", "hypothesis_revised"):
        return f"{p['hypothesis_id']} v{p['version']} fragments={p['fragment_ids']}"
    if step["event_type"] == "review_cast":
        return f"{p['hypothesis_id']} v{p['version']} {p['expert_id']} -> {p['decision']}"
    if step["event_type"] == "hypothesis_invalidated":
        return f"{p['hypothesis_id']} reason={p['reason'][:24]}… evidence={p.get('evidence_id')}"
    if step["event_type"] in ("evidence_recorded",):
        return f"{p['evidence_id']} {p['stance']} {p['target']['type']}:{p['target']['id']}"
    return json.dumps(p, ensure_ascii=False)[:70]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
